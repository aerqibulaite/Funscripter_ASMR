"""Repair only failed semantic targets in a new, provenance-linked bundle."""
import json
import re
import shutil
from pathlib import Path

from .fuse_project import load_semantics
from .models import LLMConfig, SemanticBatchV2
from .planner import Planner
from .project import atomic_json, reopen_project
from .media import sha256
from .planning_inputs import planning_inputs
from .semantic_contracts import event_identity, parse_saved_v2, validate_batch_v2


def repair_planning(project_directory, source_directory, output, transport=None):
    source, output = Path(source_directory).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("repair requires a new independent output directory")
    project, timeline, media, _ = reopen_project(project_directory)
    data, outline, manifest = load_semantics(source, project, timeline, media["duration_ms"])
    if data.get("schema_version") != "1.4" or not data["failures"]:
        raise ValueError("repair requires semantic 1.4 failures")
    failed = {row["cue_id"] for row in data["failures"]}
    lookup = {cue.cue_id: cue for cue in timeline.cues}
    jobs, covered = [], set()
    for record in json.loads((source / "request_log.json").read_text(encoding="utf-8")):
        if record["task"] != "semantic" or record["key"] in {key for key, _ in jobs}:
            continue
        key = record["key"]
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid request key")
        path = (source / "requests" / key / "request.json").resolve()
        if not path.is_relative_to(source):
            raise ValueError("request path escapes source")
        request = json.loads(path.read_text(encoding="utf-8"))
        payload = json.loads(request["messages"][-1]["content"])
        ids = {row["cue_id"] for row in payload["target_cues"]}
        if not ids & failed:
            continue
        if not ids <= failed or ids & covered:
            raise ValueError("repair request mixes accepted or overlapping targets")
        for row in payload["target_cues"] + payload["context_cues"]:
            if row["cue_id"] not in lookup or row["text"] != lookup[row["cue_id"]].text_for_analysis:
                raise ValueError("repair request text differs from project")
        jobs.append((key, payload))
        covered |= ids
    if covered != failed:
        raise ValueError("failed targets lack complete source requests")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("source contains symbolic links")
    shutil.copytree(source, output)
    atomic_json(output / "repair_source_manifest.json", manifest)
    original_log = json.loads((source / "request_log.json").read_text(encoding="utf-8"))
    atomic_json(output / "repair_source_log.json", original_log)
    manifest = dict(manifest, status="running")
    manifest.pop("error", None)
    atomic_json(output / "planning_run.json", manifest)
    snapshot = source / "semantic_knowledge.json"
    knowledge = json.loads(snapshot.read_text(encoding="utf-8"))["content"] if snapshot.is_file() else None
    planner = Planner(LLMConfig.model_validate(manifest["config"]), output, transport, knowledge=knowledge)
    events = list(data["events"])
    assessments = {row["cue_id"]: row for row in data["cue_assessments"]}
    context_notes = list(data.get("context_notes", []))
    remaining = set(failed)
    for key, payload in jobs:
        targets = payload["target_cues"]
        for start in range(0, len(targets), 4):
            selected = targets[start:start+4]
            selected_ids = {row["cue_id"] for row in selected}
            contexts = {row["cue_id"]: row for row in payload["context_cues"] + targets
                        if row["cue_id"] not in selected_ids}
            request = {**payload, "target_cues": selected, "context_cues": list(contexts.values()),
                "repair_instructions": "修复失败批次，保留原文证据。processed 必须至少有一个以该 cue_id 为锚点的事件或情境笔记；"
                "没有新增事件使用 no_new_information，含义不确定使用 ambiguous 并解释。不要为满足 processed 编造事件。"}
            request.pop("task", None)
            result = planner.call("semantic", request, SemanticBatchV2,
                lambda value: validate_batch_v2(value, [lookup[x["cue_id"]] for x in selected],
                                                [lookup[x] for x in contexts]))
            if result is None:
                continue
            for row in result.cue_assessments:
                assessments[row.cue_id] = row.model_dump(mode="json")
            context_notes.extend(row.model_dump(mode="json") for row in result.context_notes)
            for event in result.events:
                row = event.model_dump(mode="json")
                row.update(event_id=event_identity(row), review_status="model_candidate_not_human_confirmed")
                events.append(row)
            remaining -= selected_ids
    order = {cue.cue_id: i for i, cue in enumerate(timeline.cues)}
    events.sort(key=lambda row: order[row["cue_id"]])
    repaired = {"schema_version": "1.4", "events": events,
                "context_notes": context_notes,
                "cue_assessments": [assessments[cue.cue_id] for cue in timeline.cues],
                "failures": [row for row in data["failures"] if row["cue_id"] in remaining]}
    eligible, _ = planning_inputs(timeline, media["duration_ms"])
    parse_saved_v2(repaired, timeline.cues, {cue.cue_id for cue in eligible})
    if [row for row in events if row["cue_id"] not in failed] != data["events"]:
        raise ValueError("repair changed previously accepted events")
    report = {"source_directory": str(source), "source_manifest_sha256": sha256(source / "planning_run.json"),
              "implementation_sha256": sha256(__file__), "target_cue_ids": sorted(failed),
              "preserved_event_count": len(data["events"]), "repaired_count": len(failed - remaining),
              "remaining_failure_count": len(remaining),
              "context_policy": "Existing results retain their original request context; repaired events are not propagated downstream. Human continuity review required."}
    atomic_json(output / "semantic_events.json", repaired)
    atomic_json(output / "repair_report.json", report)
    manifest.update(status="failed" if remaining else "needs_review", event_count=len(events), failure_count=len(remaining),
                    repair=report, human_review_required=True)
    names = set(manifest["artifact_hashes"]) | {"repair_report.json", "repair_source_manifest.json", "repair_source_log.json"}
    manifest["artifact_hashes"] = {name: sha256(output / name) for name in names}
    atomic_json(output / "planning_run.json", manifest)
    return report
