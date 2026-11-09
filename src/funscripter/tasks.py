"""Persistent records for synchronous CLI stages; no background worker."""
import hashlib
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from .media import sha256
from .models import Model
from .project import atomic_json, load_config


# Keep replay limited to the same public operations accepted by the CLI.
FIELDS = {
    "create": {"config", "output"},
    "analyze": {"project", "output", "profile", "reuse_analysis"},
    "plan": {"project", "output", "profile", "resume"},
    "fuse": {"project", "planning", "analysis", "profile", "output"},
    "diagnostic": {"project", "draft", "profile", "output"},
    "archive-revision": {"project", "baseline", "script", "diagnostic", "output", "elapsed_seconds", "notes"},
    "diff-revision": {"project", "revision", "baseline", "profile", "output"},
    "prepare-regeneration": {"project", "diff", "profile", "output"},
    "accept-candidate": {"project", "candidate", "candidate_id", "output"},
    "restore-revision": {"project", "applied", "target", "output"},
}
PATH_FIELDS = {"config", "output", "project", "profile", "planning", "analysis", "draft",
               "baseline", "script", "diagnostic", "revision", "diff", "candidate", "applied", "reuse_analysis"}


class Task(Model):
    schema_version: str = "1"
    task_id: str
    command: str
    arguments: dict
    execution_status: Literal["queued", "running", "completed", "failed", "cancelled"]
    review_status: Literal["not_available", "needs_review"] = "not_available"
    created_at: str
    updated_at: str
    implementation_hashes: dict[str, str]
    input_hashes: dict[str, str]
    output_hashes: dict[str, str] | None = None
    attempts: list[dict] = Field(default_factory=list)
    result: dict | None = None
    error: str | None = None


def now():
    return datetime.now(timezone.utc).isoformat()


def implementation_hashes():
    return {path.name: sha256(path) for path in sorted(Path(__file__).parent.glob("*.py"))}


def tree_hashes(path):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"input or artifact missing: {path}")
    if path.is_symlink():
        raise ValueError(f"task snapshots do not support symbolic links: {path}")
    if path.is_file():
        return {str(path): sha256(path)}
    result = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"task snapshots do not support symbolic links: {item}")
        if item.is_file():
            result[str(item)] = sha256(item)
    return result


def input_paths(arguments):
    paths = [Path(value) for key, value in arguments.items()
             if key in PATH_FIELDS - {"output"} and value is not None]
    if "config" in arguments:
        config_path = Path(arguments["config"])
        config, base = load_config(config_path)
        # Include media referenced by create's YAML, not just the YAML itself.
        paths.extend((base / value).resolve() for value in (config.audio, config.subtitles.path))
    return paths


def inputs(arguments):
    result = {}
    for path in input_paths(arguments):
        result.update(tree_hashes(path))
    return result


def validate_arguments(command, arguments):
    if command not in FIELDS or set(arguments) != FIELDS[command]:
        raise ValueError("invalid task command or arguments")
    for key, value in arguments.items():
        optional = (key == "profile" and command in ("analyze", "plan")) or (
            key == "reuse_analysis" and command == "analyze") or (
            key == "analysis" and command == "fuse") or (key == "diagnostic" and command == "archive-revision")
        if key in PATH_FIELDS:
            if optional and value is None:
                continue
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("task paths must be absolute and required paths cannot be empty")
        elif key == "resume" and type(value) is not bool:
            raise ValueError("task resume must be boolean")
        elif key in {"notes", "candidate_id"} and not isinstance(value, str):
            raise ValueError(f"task {key} must be text")
        elif key == "target" and value not in ("parent", "result"):
            raise ValueError("invalid restore target")
        elif key == "elapsed_seconds" and value is not None and type(value) not in (float, int):
            raise ValueError("task elapsed_seconds must be numeric")


def validate_location(path, arguments):
    # Records/locks must never enter a hashed input or an immutable output bundle.
    output = Path(arguments["output"])
    for source in [output, *input_paths(arguments)]:
        if source != output and source.is_dir() and output.is_relative_to(source):
            raise ValueError("output must be outside input directories")
        if path == source or (source.is_dir() or source == output) and path.is_relative_to(source):
            raise ValueError("task file must be outside input and output paths")


def read_task(path):
    task = Task.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if task.command == "analyze":
        task.arguments.setdefault("reuse_analysis", None)
    if task.schema_version != "1" or task.execution_status not in {
            "queued", "running", "completed", "failed", "cancelled"}:
        raise ValueError("unsupported task schema or status")
    validate_arguments(task.command, task.arguments)
    return task


def save_task(path, task):
    task.updated_at = now()
    atomic_json(path, task.model_dump(mode="json"))


@contextmanager
def task_lock(path):
    """OS-held lock: automatically released after a crash; safe across processes."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("task is already running in another process") from None
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def task_summary(path):
    task = read_task(path)
    return {"task_file": str(Path(path).resolve()), **task.model_dump(mode="json")}


def output_lock(arguments):
    output = Path(arguments["output"])
    key = hashlib.sha256(os.path.normcase(str(output)).encode()).hexdigest()
    return task_lock(output.parent / ".funscripter-tasks" / f"output-{key}")


def run_task(command, arguments, executor, task_file=None):
    arguments = {key: str(Path(value).resolve()) if key in PATH_FIELDS and value is not None else value
                 for key, value in arguments.items()}
    validate_arguments(command, arguments)
    task_id = str(uuid.uuid4())
    output = Path(arguments["output"])
    path = (Path(task_file) if task_file else output.parent / ".funscripter-tasks" / f"{task_id}.json").resolve()
    validate_location(path, arguments)
    with task_lock(path):
        if path.exists():
            raise ValueError("task file already exists; use task-resume or choose a new task file")
        if output.exists() and not (command == "plan" and arguments.get("resume")):
            raise ValueError("output already exists; choose a new directory")
        task = Task(task_id=task_id, command=command, arguments=arguments,
                    execution_status="queued", created_at=now(), updated_at=now(),
                    implementation_hashes=implementation_hashes(), input_hashes=inputs(arguments),
                    output_hashes=tree_hashes(output) if output.exists() else None)
        save_task(path, task)
        with output_lock(arguments):
            return execute(path, task, executor)


def resume_task(path, executor):
    path = Path(path).resolve()
    with task_lock(path):
        task = read_task(path)
        validate_location(path, task.arguments)
        with output_lock(task.arguments):
            return recover_locked(path, task, executor)


def recover_locked(path, task, executor):
    if implementation_hashes() != task.implementation_hashes:
        raise ValueError("task implementation changed; start a new task")
    if inputs(task.arguments) != task.input_hashes:
        raise ValueError("task inputs/config changed; start a new task")
    output = Path(task.arguments["output"])
    if task.output_hashes is not None:
        if not output.exists() or tree_hashes(output) != task.output_hashes:
            raise ValueError("task output changed or missing; recovery refused")
    elif output.exists():
        raise ValueError("unverified output exists after interruption; preserve it and start a new task")
    if task.execution_status == "completed":
        return decorate(path, task)
    if output.exists() and task.command != "plan":
        raise ValueError("published output is preserved; review it or start a new task with a new output")
    if task.command == "plan":
        task.arguments["resume"] = output.exists()
    if task.execution_status == "running" and task.attempts:
        task.attempts[-1].update(execution_status="failed", finished_at=now(),
                                 error="previous process ended without finalizing the task")
    return execute(path, task, executor)


def decorate(path, task):
    return {**(task.result or {}), "task_id": task.task_id, "task_file": str(path),
            "execution_status": task.execution_status, "review_status": task.review_status}


def execute(path, task, executor):
    task.execution_status, task.error, task.result = "running", None, None
    task.review_status = "not_available"
    attempt = {"started_at": now(), "execution_status": "running"}
    task.attempts.append(attempt)
    save_task(path, task)
    print(f"task: {path}", file=sys.stderr, flush=True)
    try:
        task.result = executor(task.command, task.arguments)
        if inputs(task.arguments) != task.input_hashes:
            raise ValueError("task inputs/config changed during execution; preserve output and start a new task")
        status = task.result.get("status")
        task.execution_status = status if status in ("failed", "cancelled") else "completed"
        task.review_status = "needs_review"
        if status in ("failed", "cancelled"):
            task.error = str(task.result.get("error") or f"stage returned {status}")
    except KeyboardInterrupt:
        task.execution_status, task.error = "cancelled", "interrupted by user"
        task.result = {"status": "cancelled"}
    except Exception as exc:
        task.execution_status, task.error = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        attempt.update(finished_at=now(), execution_status=task.execution_status, error=task.error)
        output = Path(task.arguments["output"])
        try:
            task.output_hashes = tree_hashes(output) if output.exists() else None
        except (OSError, ValueError) as exc:
            task.execution_status, task.error = "failed", f"cannot snapshot output: {exc}"
            attempt.update(execution_status="failed", error=task.error)
            task.output_hashes = None
        save_task(path, task)
    return decorate(path, task)
