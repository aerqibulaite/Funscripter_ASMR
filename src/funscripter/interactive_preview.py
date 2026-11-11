"""Publish an offline audio/curve browser from a verified fusion revision."""
import json
import os
import shutil
import tempfile
from pathlib import Path

from .fuse_project import load_semantics, read_json, verify_bundle, verify_source
from .media import sha256
from .motion import validate_actions
from .preview import waveform
from .project import atomic_json, reopen_project


def curve_summary(actions, duration_ms):
    moving, distance, maximum, changes, previous_direction = 0, 0, 0.0, 0, 0
    holds, hold_start = [], None
    for left, right in zip(actions, actions[1:]):
        delta, elapsed = right["pos"] - left["pos"], right["at"] - left["at"]
        if delta:
            if hold_start is not None:
                holds.append({"start_ms": hold_start, "end_ms": left["at"]})
                hold_start = None
            moving += elapsed
            distance += abs(delta)
            maximum = max(maximum, abs(delta) * 1000 / elapsed)
            direction = 1 if delta > 0 else -1
            changes += bool(previous_direction and direction != previous_direction)
            previous_direction = direction
        elif hold_start is None:
            hold_start = left["at"]
    if hold_start is not None:
        holds.append({"start_ms": hold_start, "end_ms": actions[-1]["at"]})
    return {"moving_ms": moving, "holding_ms": duration_ms - moving,
            "longest_hold_ms": max((row["end_ms"] - row["start_ms"] for row in holds), default=0),
            "max_speed": maximum, "travel": distance, "direction_changes": changes,
            "action_count": len(actions)}, holds


def create_interactive_preview(directory, draft, output, planning=None):
    directory, draft, output = map(lambda path: Path(path).resolve(), (directory, draft, output))
    planning = Path(planning).resolve() if planning else None
    if output.exists():
        raise ValueError("preview output already exists; choose a new directory")
    if any(output.is_relative_to(path) or path.is_relative_to(output)
           for path in [directory, draft] + ([planning] if planning else [])):
        raise ValueError("preview output must be independent of source directories")
    project, timeline, media, _ = reopen_project(directory)
    manifest = read_json(draft / "export_manifest.json")
    verify_source(manifest, project, "subtitle_sha256")
    verify_bundle(draft, manifest, "artifact_hashes")
    if not {"segment_plan.json", "generated_base.json", "validation.json"} <= set(manifest["artifact_hashes"]):
        raise ValueError("draft lacks hashed plan, candidate curve or validation")
    plan = read_json(draft / "segment_plan.json")
    script = read_json(draft / "generated_base.json")
    validation = read_json(draft / "validation.json")
    checked = validate_actions(script["actions"], media["duration_ms"], project.config.device)
    actions = script["actions"]
    if (not checked["format_valid"] or len(actions) < 2 or actions[0]["at"] != 0
            or actions[-1]["at"] != media["duration_ms"]):
        raise ValueError("preview requires a valid full-duration curve")
    semantic = {"events": [], "context_notes": []}
    if planning:
        if sha256(planning / "planning_run.json") != manifest["planning_manifest_sha256"]:
            raise ValueError("preview planning does not match fused revision")
        semantic, _, _ = load_semantics(planning, project, timeline, media["duration_ms"])
    stats, holds = curve_summary(actions, media["duration_ms"])
    problems = []
    for row in holds:
        if row["end_ms"] - row["start_ms"] >= 10000:
            problems.append({**row, "kind": "长时间静止", "detail": "曲线连续静止至少 10 秒；是否合理需结合台词判断"})
    cues = {cue.cue_id: cue for cue in timeline.cues}
    for row in plan["event_dispositions"]:
        if row["status"] in ("unresolved", "conflict", "failed_semantics", "ambiguous"):
            cue = cues.get(row["cue_id"])
            time = row.get("effective_time_ms", cue.start_ms if cue else 0)
            problems.append({"start_ms": max(0, time), "end_ms": min(media["duration_ms"], cue.end_ms or time) if cue else time,
                             "kind": row["status"], "detail": "; ".join(row["reasons"])})
    problems.sort(key=lambda row: (row["start_ms"], row["kind"]))
    data = {"duration_ms": media["duration_ms"], "revision_id": manifest["revision_id"],
            "export_blocked": bool(validation.get("export_blocked") or checked["constraint_errors"]),
            "constraint_errors": checked["constraint_errors"], "statistics": stats, "actions": actions,
            "segments": plan["segments"], "cues": [cue.model_dump(mode="json") for cue in timeline.cues],
            "events": semantic["events"], "context_notes": semantic.get("context_notes", plan.get("context_notes", [])),
            "has_semantics": bool(planning), "problems": problems,
            "interpolation": "linear_preview", "source_interpolation": project.config.device.final_interpolation}
    low, high = waveform(directory / project.artifacts["analysis_audio"], 0, media["duration_ms"], 4096)
    data["waveform"] = {"low": low.round(4).tolist(), "high": high.round(4).tolist()}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-preview-", dir=output.parent))
    try:
        assets = Path(__file__).with_name("preview_assets")
        template = (assets / "index.html").read_text(encoding="utf-8")
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
        (stage / "index.html").write_text(template.replace("__PREVIEW_DATA__", encoded), encoding="utf-8")
        for name in ("viewer.js", "viewer.css"):
            shutil.copyfile(assets / name, stage / name)
        shutil.copyfile(directory / project.artifacts["analysis_audio"], stage / "audio.wav")
        result = {"schema_version": "1", "status": "needs_review", "revision_id": manifest["revision_id"],
                  "project_id": project.project_id, "source_manifest_sha256": sha256(draft / "export_manifest.json"),
                  "statistics": stats, "problem_count": len(problems), "export_blocked": data["export_blocked"],
                  "artifact_hashes": {name: sha256(stage / name) for name in ("index.html", "viewer.js", "viewer.css", "audio.wav")}}
        atomic_json(stage / "preview_manifest.json", result)
        os.replace(stage, output)
        return {**result, "output": str(output), "html": str(output / "index.html")}
    except BaseException:
        shutil.rmtree(stage)
        raise
