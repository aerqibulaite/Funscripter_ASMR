"""Serial, dependency-aware batches built from existing persistent CLI tasks."""
import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from .models import Model
from .project import atomic_json
from .tasks import (FIELDS, PATH_FIELDS, implementation_hashes, now, read_task,
                    resume_task, run_task, task_lock, validate_arguments)

DEFAULTS = {"profile": None, "reuse_analysis": None, "analysis": None, "diagnostic": None, "boundaries": None, "vad_evidence": None,
            "resume": False, "elapsed_seconds": None, "notes": ""}
FILE_INPUTS = {"config", "profile", "script", "vad_evidence"}
Status = Literal["queued", "running", "completed", "failed", "cancelled", "blocked"]


class Job(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    command: str
    arguments: dict
    depends_on: list[str] = Field(default_factory=list)


class BatchConfig(Model):
    schema_version: Literal["1"] = "1"
    failure_policy: Literal["stop", "continue"] = "stop"
    jobs: list[Job] = Field(min_length=1)


class Item(Job):
    execution_status: Status = "queued"
    task_id: str | None = None
    result: dict | None = None
    error: str | None = None


class Batch(Model):
    schema_version: Literal["1"] = "1"
    config_file: str
    config_sha256: str
    implementation_hashes: dict[str, str]
    failure_policy: Literal["stop", "continue"]
    execution_status: Status = "queued"
    created_at: str
    updated_at: str
    attempts: list[dict] = Field(default_factory=list)
    items: list[Item]


def load_config(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        config = BatchConfig.model_validate(yaml.safe_load(raw))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid batch YAML: {exc}") from None
    known, ancestors = set(), {}
    for job in config.jobs:
        if job.id in known or len(set(job.depends_on)) != len(job.depends_on):
            raise ValueError("batch job IDs and dependencies must be unique")
        if not set(job.depends_on).issubset(known):
            raise ValueError("dependencies must refer to earlier jobs: " + job.id)
        if job.command not in FIELDS or set(job.arguments) - FIELDS[job.command]:
            raise ValueError("invalid batch command or arguments: " + job.id)
        arguments = {key: value for key, value in DEFAULTS.items() if key in FIELDS[job.command]}
        arguments.update(job.arguments)
        for key, value in arguments.items():
            if key in PATH_FIELDS and value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("batch paths must be nonempty strings: " + job.id)
                arguments[key] = str((path.parent / value).resolve())
        validate_arguments(job.command, arguments)
        job.arguments = arguments
        ancestors[job.id] = set(job.depends_on)
        for dependency in job.depends_on:
            ancestors[job.id].update(ancestors[dependency])
        known.add(job.id)
    outputs = {job.id: Path(job.arguments["output"]) for job in config.jobs}
    for index, job in enumerate(config.jobs):
        output = outputs[job.id]
        for previous in config.jobs[:index]:
            other = outputs[previous.id]
            if output.is_relative_to(other) or other.is_relative_to(output):
                raise ValueError("batch outputs must be distinct and non-nested")
        for key, value in job.arguments.items():
            if key not in PATH_FIELDS - {"output"} or value is None:
                continue
            source = Path(value)
            for producer, produced in outputs.items():
                if source.is_relative_to(produced) and producer not in ancestors[job.id]:
                    raise ValueError(f"job {job.id} must depend on earlier producer {producer}")
                if key not in FILE_INPUTS and produced != source and produced.is_relative_to(source):
                    raise ValueError("batch output cannot be nested inside a source directory")
    return config, hashlib.sha256(raw).hexdigest()


def metadata(directory):
    return Path(directory).resolve() / ".batch"


def validate_location(directory, config):
    record = metadata(directory)
    for job in config.jobs:
        for key, value in job.arguments.items():
            if key not in PATH_FIELDS or value is None:
                continue
            path = Path(value)
            if path.is_relative_to(record) or (key not in FILE_INPUTS and record.is_relative_to(path)):
                raise ValueError("batch metadata must be outside job input/output paths")


def save(directory, batch):
    batch.updated_at = now()
    atomic_json(metadata(directory) / "state.json", batch.model_dump(mode="json"))


def summary(directory, batch):
    counts = {status: sum(item.execution_status == status for item in batch.items)
              for status in ("queued", "running", "completed", "failed", "cancelled", "blocked")}
    return {"batch_directory": str(Path(directory).resolve()), **batch.model_dump(mode="json"),
            "review_status": "needs_review" if counts["completed"] else "not_available", "counts": counts}


def batch_status(directory):
    batch = Batch.model_validate_json((metadata(directory) / "state.json").read_text(encoding="utf-8"))
    return summary(directory, batch)


def run_batch(config_path, directory, executor):
    directory = Path(directory).resolve()
    config, digest = load_config(config_path)
    validate_location(directory, config)
    # mkdir is exclusive; an existing directory is never adopted as a new batch.
    directory.mkdir(parents=True, exist_ok=False)
    batch = Batch(config_file=str(Path(config_path).resolve()), config_sha256=digest,
        implementation_hashes=implementation_hashes(), failure_policy=config.failure_policy,
        created_at=now(), updated_at=now(), items=[Item(**job.model_dump()) for job in config.jobs])
    with task_lock(metadata(directory) / "state.json"):
        save(directory, batch)
        return execute(directory, batch, executor)


def resume_batch(directory, executor):
    directory = Path(directory).resolve()
    if not (metadata(directory) / "state.json").is_file():
        raise ValueError("batch record not found")
    with task_lock(metadata(directory) / "state.json"):
        batch = Batch.model_validate_json((metadata(directory) / "state.json").read_text(encoding="utf-8"))
        config, digest = load_config(batch.config_file)
        validate_location(directory, config)
        jobs = [Job(id=item.id, command=item.command, arguments=item.arguments,
                    depends_on=item.depends_on).model_dump() for item in batch.items]
        if (digest != batch.config_sha256 or config.failure_policy != batch.failure_policy
                or [job.model_dump() for job in config.jobs] != jobs):
            raise ValueError("batch configuration changed; start a new batch")
        if implementation_hashes() != batch.implementation_hashes:
            raise ValueError("batch implementation changed; start a new batch")
        return execute(directory, batch, executor)


def task_arguments_match(item, task):
    original, saved = dict(item.arguments), dict(task.arguments)
    if item.command == "plan":
        original.pop("resume", None)
        saved.pop("resume", None)
    return task.command == item.command and original == saved


def execute(directory, batch, executor):
    if batch.execution_status == "running" and batch.attempts:
        batch.attempts[-1].update(execution_status="failed", finished_at=now(), error="previous process interrupted")
    attempt = {"started_at": now(), "execution_status": "running"}
    batch.attempts.append(attempt)
    batch.execution_status = "running"
    # Revalidate every completed child; old success alone is not proof of current inputs.
    for item in batch.items:
        item.execution_status, item.error, item.result = "queued", None, None
    save(directory, batch)
    items = {item.id: item for item in batch.items}
    cancelled = False
    try:
        for item in batch.items:
            if any(items[key].execution_status != "completed" for key in item.depends_on):
                item.execution_status, item.error = "blocked", "dependency did not complete"
                save(directory, batch)
                continue
            path = metadata(directory) / "tasks" / f"{item.id}.json"
            item.execution_status, item.error = "running", None
            save(directory, batch)
            try:
                if path.exists():
                    task = read_task(path)
                    if not task_arguments_match(item, task) or (item.task_id and item.task_id != task.task_id):
                        raise ValueError("batch child task identity/arguments changed")
                    result = resume_task(path, executor)
                else:
                    if item.task_id:
                        raise ValueError("batch child task record missing; recovery refused")
                    result = run_task(item.command, item.arguments, executor, path)
                item.result, item.task_id = result, result["task_id"]
                item.execution_status = result["execution_status"]
                if item.execution_status in ("failed", "cancelled"):
                    item.error = read_task(path).error
            except KeyboardInterrupt:
                item.execution_status, item.error = "cancelled", "interrupted by user"
            except Exception as exc:
                item.execution_status, item.error = "failed", f"{type(exc).__name__}: {exc}"
                if path.exists() and item.task_id is None:
                    try:
                        item.task_id = read_task(path).task_id
                    except (OSError, ValueError):
                        pass
            save(directory, batch)
            if item.execution_status == "cancelled":
                cancelled = True
                break
            if item.execution_status == "failed" and batch.failure_policy == "stop":
                break
    except KeyboardInterrupt:
        cancelled = True
    finally:
        # Preserve independent pending jobs; only failed dependency chains are blocked.
        for item in batch.items:
            if item.execution_status == "running":
                item.execution_status = "cancelled" if cancelled else "failed"
                item.error = "batch interrupted before child task finalized"
            if item.execution_status in ("queued", "blocked") and any(
                    items[key].execution_status in ("failed", "cancelled", "blocked") for key in item.depends_on):
                item.execution_status, item.error = "blocked", "dependency did not complete"
        batch.execution_status = "cancelled" if cancelled else (
            "completed" if all(item.execution_status == "completed" for item in batch.items) else "failed")
        attempt.update(finished_at=now(), execution_status=batch.execution_status)
        save(directory, batch)
    return summary(directory, batch)
