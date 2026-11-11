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


def recover_outline(project_directory, source_directory, output, transport=None):
    """Reuse validated source partials and retry only hierarchical merging."""
    from .models import Outline
    from .planner import validate_outline
    source, output = Path(source_directory).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("outline recovery requires a new independent directory")
    project, timeline, media, _ = reopen_project(project_directory)
    data, outline, manifest = load_semantics(source, project, timeline, media["duration_ms"])
    if outline is not None or data["events"] or manifest.get("global_input_strategy") != "hierarchical_merge_failed":
        raise ValueError("outline recovery requires a failed hierarchical merge, without semantic events")
    eligible, input_view = planning_inputs(timeline, media["duration_ms"])
    lookup = {cue.cue_id: cue for cue in eligible}
    indices = {cue.cue_id: index for index, cue in enumerate(eligible)}
    knowledge = json.loads((source / "semantic_knowledge.json").read_text(encoding="utf-8"))["content"]
    planner = Planner(LLMConfig.model_validate(manifest["config"]), output, transport, knowledge=knowledge,
                      input_kind=project.config.subtitles.source_kind)
    planner.quality_flags = {row["cue_id"]: row["quality_flags"] for row in input_view["cues"]}
    summaries, seen, provenance = [], set(), []
    for record in json.loads((source / "request_log.json").read_text(encoding="utf-8")):
        key = record["key"]
        if record["task"] != "outline_partial" or record["status"] not in ("accepted", "cached") or key in seen:
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid partial outline request key")
        folder = (source / "requests" / key).resolve()
        if not folder.is_relative_to(source):
            raise ValueError("partial outline path escapes source")
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        payload = json.loads(request["messages"][-1]["content"])
        group = [lookup[row["cue_id"]] for row in payload["cues"]]
        recorded_cues = [dict(row) for row in payload["cues"]]
        if project.config.subtitles.source_kind == "subtitles":
            for row in recorded_cues:
                row.setdefault("input_kind", "subtitles")  # Compatible with pre-transcription checkpoints.
        if payload["task"] != "outline_partial" or recorded_cues != [planner.cue_data(cue) for cue in group]:
            raise ValueError("partial outline input differs from project")
        accepted = folder / "accepted.json"
        partial = Outline.model_validate_json(accepted.read_text(encoding="utf-8"))
        validate_outline(partial, group)
        summaries.append((partial, group))
        provenance.append({"request_key": key, "accepted_sha256": sha256(accepted)})
        seen.add(key)
    summaries.sort(key=lambda pair: indices[pair[1][0].cue_id])
    if [cue.cue_id for _, group in summaries for cue in group] != [cue.cue_id for cue in eligible]:
        raise ValueError("accepted partial outlines do not cover source cues exactly")
    output.mkdir(parents=True)
    atomic_json(output / "source_partials.json", [{"outline": partial.model_dump(mode="json"),
                "cue_ids": [cue.cue_id for cue in group]} for partial, group in summaries])
    result, strategy = planner.merge_summaries(summaries)
    if result is not None:
        validate_outline(result, eligible)
        atomic_json(output / "outline.json", result.model_dump(mode="json"))
    report = {"status": "outline_recovered_semantics_pending" if result is not None else "failed",
        "source_manifest_sha256": sha256(source / "planning_run.json"), "source_directory": str(source),
        "project_id": project.project_id, "source_revision_id": project.revision_id,
        "subtitle_sha256": project.artifact_hashes["subtitles"], "knowledge_sha256": planner.knowledge_hash,
        "reused_partial_count": len(summaries), "covered_cue_count": len(eligible), "strategy": strategy,
        "source_partials": provenance, "semantic_planning_complete": False}
    atomic_json(output / "request_log.json", planner.trace)
    report["artifact_hashes"] = {name: sha256(output / name) for name in
        ("outline.json", "source_partials.json", "request_log.json") if (output / name).exists()}
    atomic_json(output / "outline_recovery.json", report)
    if result is not None:
        import yaml
        config = planner.config.model_copy(update={"outline_seed_dir": str(output)}).model_dump(mode="json")
        (output / "llm-recovered.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return report


def load_outline_seed(directory, project, cues, knowledge_hash):
    from .fuse_project import verify_bundle
    from .models import Outline
    from .planner import validate_outline
    directory = Path(directory).resolve()
    path = directory / "outline_recovery.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("status") != "outline_recovered_semantics_pending"
            or report.get("project_id") != project.project_id or report.get("source_revision_id") != project.revision_id
            or report.get("subtitle_sha256") != project.artifact_hashes["subtitles"]
            or report.get("knowledge_sha256") != knowledge_hash):
        raise ValueError("outline seed source/knowledge mismatch; clear outline_seed_dir or choose matching recovery")
    verify_bundle(directory, report, "artifact_hashes")
    if "outline.json" not in report["artifact_hashes"]:
        raise ValueError("outline seed is not covered by artifact hashes")
    outline = Outline.model_validate_json((directory / "outline.json").read_text(encoding="utf-8"))
    validate_outline(outline, cues)
    return outline, sha256(path)


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
            # Adaptive subdivision leaves rejected parent requests in the log.
            # Select only requests wholly owned by remaining failed targets.
            continue
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
    planner = Planner(LLMConfig.model_validate(manifest["config"]), output, transport, knowledge=knowledge,
                      input_kind=project.config.subtitles.source_kind)
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
