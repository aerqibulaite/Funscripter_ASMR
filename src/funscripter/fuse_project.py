"""Validate saved source revisions and publish an independent fused draft."""
import csv
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
import yaml
from .models import FusionProfile, Outline, SemanticEvent, SemanticBatch, SegmentPlan
from .planner import validate_events, validate_outline
from .fusion import build_plan, event_identity
from .media import sha256
from .project import atomic_json, reopen_project
from .preview import render_previews


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def verify_bundle(directory, manifest, hash_field):
    hashes = manifest.get(hash_field)
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("source manifest has no artifact hashes")
    for relative, expected in hashes.items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or sha256(path) != expected:
            raise ValueError("source artifact changed or escapes bundle: " + relative)


def verify_source(manifest, project, subtitle_key):
    if manifest.get("project_id") != project.project_id or manifest.get("source_revision_id") != project.revision_id:
        raise ValueError("source project/revision mismatch")
    if manifest.get(subtitle_key) != project.artifact_hashes["subtitles"]:
        raise ValueError("source subtitle hash mismatch")


def load_semantics(directory, project, timeline):
    manifest = read_json(directory / "planning_run.json")
    if manifest.get("status") not in ("needs_review", "failed"):
        raise ValueError("planning must be finished before fusion")
    verify_source(manifest, project, "subtitle_sha256")
    verify_bundle(directory, manifest, "artifact_hashes")
    if "semantic_events.json" not in manifest["artifact_hashes"]:
        raise ValueError("semantic events are not covered by source hashes")
    data = read_json(directory / "semantic_events.json")
    events, failures = data.get("events", []), data.get("failures", [])
    lookup = {cue.cue_id: cue for cue in timeline.cues}
    ids = [event["cue_id"] for event in events]
    failed_ids = [failure["cue_id"] for failure in failures]
    if (len(ids) != len(set(ids)) or len(failed_ids) != len(set(failed_ids)) or set(ids) & set(failed_ids)
            or set(ids) | set(failed_ids) != set(lookup)):
        raise ValueError("semantic event/failure coverage is missing or duplicated")
    parsed = []
    for event in events:
        payload = {key: value for key, value in event.items() if key not in ("event_id", "review_status")}
        parsed.append(SemanticEvent.model_validate(payload))
        if event.get("event_id") != event_identity(event):
            raise ValueError("semantic event identity mismatch")
    validate_events(SemanticBatch(schema_version="1.3", events=parsed), [lookup[cue_id] for cue_id in ids])
    if any(not isinstance(failure.get("reason"), str) or not failure["reason"] for failure in failures):
        raise ValueError("semantic failures need explicit reasons")
    outline = None
    if "outline.json" in manifest["artifact_hashes"]:
        outline = Outline.model_validate_json((directory / "outline.json").read_text(encoding="utf-8"))
        validate_outline(outline, timeline.cues)
    elif events:
        raise ValueError("semantic events need a validated global outline")
    return data, outline, manifest


def fuse_project(directory, planning, output, profile_path, analysis=None, boundaries=None):
    directory, planning, output = Path(directory).resolve(), Path(planning).resolve(), Path(output).resolve()
    analysis = Path(analysis).resolve() if analysis else None
    boundaries = Path(boundaries).resolve() if boundaries else None
    if output.exists():
        raise ValueError("fusion output already exists; choose a new directory")
    if any(output.is_relative_to(path) for path in [directory, planning] + ([analysis] if analysis else []) + ([boundaries] if boundaries else [])):
        raise ValueError("fusion output must be outside source directories")
    project, timeline, media, _ = reopen_project(directory)
    overrides = read_json(directory / project.artifacts["overrides"])
    if overrides.get("overrides") or overrides.get("locked_ranges"):
        raise ValueError("manual curve edits/locks require revision protection; fusion cannot overwrite them")
    profile = FusionProfile.model_validate_json(json.dumps(yaml.safe_load(Path(profile_path).read_text(encoding="utf-8-sig")), allow_nan=False))
    semantic, outline, semantic_manifest = load_semantics(planning, project, timeline)
    audio, audio_manifest = None, None
    if analysis:
        audio_manifest = read_json(analysis / "analysis_manifest.json")
        verify_source(audio_manifest, project, "summary_subtitle_sha256")
        verify_bundle(analysis, audio_manifest, "artifacts")
        if "audio_evidence.json" not in audio_manifest["artifacts"]:
            raise ValueError("audio evidence is not covered by source hashes")
        audio = read_json(analysis / "audio_evidence.json")
        verify_source(audio, project, "subtitle_sha256")
        if audio.get("input_sha256") != project.artifact_hashes["analysis_audio"] or audio.get("sample_count") != media["sample_count"]:
            raise ValueError("audio evidence does not match analysis media")
    candidates = read_json(directory / project.artifacts["candidates"])
    if boundaries:
        from .boundaries import CandidateDocument, validate_document
        boundary_manifest = read_json(boundaries / "boundary_manifest.json")
        verify_source(boundary_manifest, project, "subtitle_sha256")
        verify_bundle(boundaries, boundary_manifest, "artifact_hashes")
        if ("utterance_candidates.json" not in boundary_manifest["artifact_hashes"]
                or boundary_manifest.get("analysis_audio_sha256") != project.artifact_hashes["analysis_audio"]):
            raise ValueError("boundary bundle does not match project audio")
        if analysis and boundary_manifest.get("analysis_manifest_sha256") != sha256(analysis / "analysis_manifest.json"):
            raise ValueError("boundary bundle uses a different analysis revision")
        document = CandidateDocument.model_validate_json((boundaries / "utterance_candidates.json").read_text(encoding="utf-8"))
        validate_document(document, timeline, media["duration_ms"])
        candidates = document.model_dump(mode="json")
    plan, candidates, script, validation = build_plan(project, timeline, media["duration_ms"], semantic,
                                                     outline, audio, candidates, profile)
    plan = SegmentPlan.model_validate_json(json.dumps(plan, allow_nan=False)).model_dump(mode="json")
    revision_id = str(uuid.uuid4())
    validation["export_blocked"] = bool(validation["constraint_errors"])
    validation["status"] = "constraint_failure" if validation["export_blocked"] else "offline_draft_needs_review"
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-fusion-", dir=output.parent))
    try:
        payloads = {"segment_plan.json": plan, "utterance_candidates.json": candidates,
                    "generated_base.json": script, "validation.json": validation,
                    "state_transitions.json": plan["state_transitions"]}
        if not validation["export_blocked"]:
            payloads["track.funscript"] = script
        for name, payload in payloads.items():
            atomic_json(stage / name, payload)
        with (stage / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["revision_id", "segment_id", "start_ms", "end_ms", "mode", "preset_id", "cue_ids", "event_ids", "audio_evidence_ids", "reasons"])
            for segment in plan["segments"]:
                writer.writerow([revision_id, segment["segment_id"], segment["start_ms"], segment["end_ms"],
                    segment["mode"], segment["preset_id"], "|".join(segment["cue_ids"]), "|".join(segment["event_ids"]),
                    "|".join(segment["audio_evidence_ids"]), "|".join(segment["review_reasons"])])
        pages = render_previews(stage, directory / project.artifacts["analysis_audio"], plan, script, audio,
                                media["duration_ms"], profile.preview_page_ms, revision_id, len(timeline.cues), validation["export_blocked"])
        artifacts = [*payloads, "review.csv", *pages]
        manifest = {"schema_version": "1.3", "revision_id": revision_id,
            "project_id": project.project_id, "source_revision_id": project.revision_id,
            "status": "failed" if validation["export_blocked"] else "needs_review",
            "script_exported": not validation["export_blocked"], "human_review_required": True,
            "subtitle_sha256": project.artifact_hashes["subtitles"],
            "audio_sha256": project.input_hashes["audio"],
            "planning_manifest_sha256": sha256(planning / "planning_run.json"),
            "analysis_manifest_sha256": sha256(analysis / "analysis_manifest.json") if analysis else None,
            "boundary_manifest_sha256": sha256(boundaries / "boundary_manifest.json") if boundaries else None,
            "fusion_profile_sha256": sha256(profile_path),
            "implementation_hashes": {name: sha256(Path(__file__).with_name(name)) for name in
                ["fusion.py", "motion.py", "models.py", "preview.py", "fuse_project.py"]},
            "artifact_hashes": {name: sha256(stage / name) for name in artifacts}}
        atomic_json(stage / "export_manifest.json", manifest)
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return manifest
