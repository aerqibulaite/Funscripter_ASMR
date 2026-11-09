"""Archive full external revisions with an exact baseline; never merge or overwrite."""
import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .project import reopen_project, atomic_json
from .fuse_project import read_json, verify_bundle, verify_source
from .media import sha256
from .motion import validate_actions


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key]=value
    return result


def reject_constant(value):
    raise ValueError("non-JSON numeric constant")


def validate_external(raw, duration_ms, device):
    try:
        data = json.loads(raw.decode("utf-8-sig"),object_pairs_hook=unique_object,parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        return {"format_valid":False,"errors":["invalid_json_or_encoding"],"device_constraints_passed":False}, None
    if not isinstance(data,dict) or not isinstance(data.get("actions"),list) or not data["actions"]:
        return {"format_valid":False,"errors":["missing_or_empty_actions"],"device_constraints_passed":False}, data
    if ("inverted" in data and type(data["inverted"]) is not bool) or ("range" in data and (type(data["range"]) is not int or not 0<=data["range"]<=100)):
        return {"format_valid":False,"errors":["invalid_common_format_fields"],"device_constraints_passed":False}, data
    for point in data["actions"]:
        if not isinstance(point,dict) or type(point.get("at")) is not int or type(point.get("pos")) is not int:
            return {"format_valid":False,"errors":["action_time_and_position_must_be_integers"],"device_constraints_passed":False}, data
    report = validate_actions(data["actions"],duration_ms,device)
    report["errors"] = list(report["constraint_errors"])
    report["interpolation_assumption"] = "baseline profile; external player settings not confirmed"
    return report, data


def archive_revision(directory, baseline_directory, edited_path, output, diagnostic=None, elapsed_seconds=None, notes=""):
    directory,baseline_directory,edited_path,output = [Path(path).resolve() for path in [directory,baseline_directory,edited_path,output]]
    diagnostic = Path(diagnostic).resolve() if diagnostic else None
    if elapsed_seconds is not None and (not math.isfinite(elapsed_seconds) or elapsed_seconds<0):
        raise ValueError("elapsed_seconds must be finite and nonnegative")
    if output.exists() or any(output.is_relative_to(path) for path in [directory,baseline_directory]+([diagnostic] if diagnostic else [])):
        raise ValueError("revision output must be a new directory outside its sources")
    project,_,media,_ = reopen_project(directory)
    baseline = read_json(baseline_directory / "export_manifest.json")
    verify_source(baseline,project,"subtitle_sha256")
    verify_bundle(baseline_directory,baseline,"artifact_hashes")
    if not baseline.get("script_exported") or "track.funscript" not in baseline["artifact_hashes"]:
        raise ValueError("external revision requires a known exported baseline")
    diagnostic_info = None
    if diagnostic:
        diagnostic_info = read_json(diagnostic / "diagnostic_manifest.json")
        verify_source(diagnostic_info,project,"subtitle_sha256")
        verify_bundle(diagnostic,diagnostic_info,"artifact_hashes")
        if diagnostic_info.get("fusion_revision_id") != baseline["revision_id"] or diagnostic_info.get("script_sha256") != baseline["artifact_hashes"]["track.funscript"]:
            raise ValueError("diagnostic belongs to a different baseline")
    original = edited_path.read_bytes()
    validation,data = validate_external(original,media["duration_ms"],project.config.device)
    source_baseline = baseline_directory / "track.funscript"
    base_data = read_json(source_baseline)
    format_fields_changed = (None if not isinstance(data,dict) else
        {key:value for key,value in data.items() if key!="actions"} != {key:value for key,value in base_data.items() if key!="actions"})
    output.parent.mkdir(parents=True,exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-revision-",dir=output.parent))
    try:
        (stage / "external_original.funscript").write_bytes(original)
        shutil.copyfile(source_baseline,stage / "baseline.funscript")
        atomic_json(stage / "validation.json",validation)
        unchanged = sha256(stage / "external_original.funscript") == baseline["artifact_hashes"]["track.funscript"]
        metadata = {"schema_version":"1.3","revision_id":str(uuid.uuid4()),"status":"needs_review",
            "created_at":datetime.now(timezone.utc).isoformat(),"project_id":project.project_id,
            "source_revision_id":project.revision_id,"baseline_fusion_revision_id":baseline["revision_id"],
            "baseline_manifest_sha256":sha256(baseline_directory / "export_manifest.json"),
            "baseline_script_sha256":baseline["artifact_hashes"]["track.funscript"],
            "inherited_protected_ranges":baseline.get("protected_ranges",[]),
            "inherited_locked_ranges":baseline.get("locked_ranges",[]),
            "original_audio_sha256":project.input_hashes["audio"],"source_external_path":str(edited_path),
            "editor_label":"OFS (user-supplied revision; app use not automatically verified)",
            "user_reported_edit_seconds":elapsed_seconds,"notes":notes,
            "byte_identical_to_baseline":unchanged,"format_fields_changed":format_fields_changed,
            "external_format_valid":validation["format_valid"],"device_constraints_passed":validation.get("device_constraints_passed",False),
            "diagnostic_manifest_sha256":sha256(diagnostic / "diagnostic_manifest.json") if diagnostic else None,
            "diagnostic_curve_status":("baseline_curve_unchanged" if unchanged else "obsolete_for_external_revision") if diagnostic_info else "unbound",
            "diagnostic_evidence_scope":"generated_baseline_only",
            "protection":{"scope":"entire_external_revision","start_ms":0,"end_ms":media["duration_ms"],
                          "automatic_merge_allowed":False,"local_edit_intent_inferred":False},
            "implementation_sha256":sha256(__file__),
            "artifact_hashes":{path.name:sha256(path) for path in stage.iterdir() if path.is_file()}}
        atomic_json(stage / "revision_manifest.json",metadata)
        os.replace(stage,output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return metadata
