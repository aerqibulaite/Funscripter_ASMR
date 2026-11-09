"""Immutable difference, proposal, acceptance and restoration packets."""
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import yaml
from .models import DiffProfile,RegenerationProfile,GenerationConfig,Segment,TimeRange
from .project import atomic_json,reopen_project
from .fuse_project import read_json,verify_bundle,verify_source
from .revisions import validate_external
from .revision_diff import compare_scripts,equal_on_ranges,complement,merged_ranges
from .revision_preview import draw_comparison
from .media import sha256
from .motion import generate,validate_actions


@contextmanager
def stage_output(output,sources):
    output=Path(output).resolve()
    if output.exists() or any(output.is_relative_to(Path(source).resolve()) for source in sources):
        raise ValueError("output must be a new directory outside source packets")
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".funscripter-revision-work-",dir=output.parent))
    try:
        yield stage
        os.replace(stage,output)
    except BaseException:
        shutil.rmtree(stage)
        raise


def write_manifest(stage,name,manifest):
    manifest["artifact_hashes"]={path.name:sha256(path) for path in stage.iterdir() if path.is_file()}
    manifest["implementation_hashes"]={name:sha256(Path(__file__).with_name(name)) for name in
        ["revision_workflow.py","revision_diff.py","revisions.py","revision_preview.py","motion.py","models.py","fuse_project.py","project.py"]}
    atomic_json(stage/name,manifest)
    return manifest


def load_archive(project,directory):
    directory=Path(directory).resolve()
    manifest=read_json(directory/"revision_manifest.json")
    if manifest.get("project_id")!=project.project_id or manifest.get("source_revision_id")!=project.revision_id:
        raise ValueError("external archive project/revision mismatch")
    if manifest.get("original_audio_sha256")!=project.input_hashes["audio"]:
        raise ValueError("external archive audio mismatch")
    verify_bundle(directory,manifest,"artifact_hashes")
    if not {"baseline.funscript","external_original.funscript"}<=set(manifest["artifact_hashes"]):
        raise ValueError("archive is missing hashed baseline/original files")
    if sha256(directory/"baseline.funscript")!=manifest.get("baseline_script_sha256"):
        raise ValueError("archived baseline hash mismatch")
    return manifest


def load_packet(project,directory,name):
    directory=Path(directory).resolve()
    manifest=read_json(directory/name)
    verify_source(manifest,project,"subtitle_sha256")
    verify_bundle(directory,manifest,"artifact_hashes")
    return manifest


def decoded(raw,duration,device):
    report,data=validate_external(raw,duration,device)
    return data if report["format_valid"] else None



def inherited_protection(report,archive,duration):
    entries=archive.get("inherited_protected_ranges",[])+archive.get("inherited_locked_ranges",[])
    report["inherited_protection_count"]=len(entries)
    report["inherited_protection"]=entries
    if not entries:
        return report
    report["comparison_protected_ranges_before_inheritance"]=deepcopy(report["protected_ranges"])
    ranges=[(row["start_ms"],row["end_ms"]) for row in report["protected_ranges"]]
    for entry in entries:
        row=TimeRange.model_validate({key:entry[key] for key in ["start_ms","end_ms"]})
        if row.end_ms>duration:
            raise ValueError("inherited protection exceeds media duration")
        ranges.append((row.start_ms,row.end_ms))
    report["protected_ranges"]=[{"start_ms":start,"end_ms":end,"guard_times_ms":[start,end],
        "reason":"diff_and_inherited_manual_protection"} for start,end in merged_ranges(ranges)]
    if report["automatic_merge_allowed"]:
        report["confirmed_unaffected_ranges"]=[{"start_ms":start,"end_ms":end} for start,end in complement(ranges,duration)]
        report["automatic_merge_allowed"]=bool(report["confirmed_unaffected_ranges"])
    report["inherited_protection_count"]=len(entries)
    return report


def project_locks(directory,project,duration):
    overrides=read_json(Path(directory)/project.artifacts["overrides"])
    if overrides.get("overrides"):
        return [TimeRange(start_ms=0,end_ms=duration)]
    result=[]
    for raw in overrides.get("locked_ranges",[]):
        if isinstance(raw,list) and len(raw)==2:
            raw={"start_ms":raw[0],"end_ms":raw[1]}
        row=TimeRange.model_validate(raw)
        if row.end_ms>duration:
            raise ValueError("stored lock exceeds media duration")
        result.append(row)
    return result

def diff_revision(directory,revision,baseline,profile_path,output):
    directory,revision,baseline=[Path(value).resolve() for value in [directory,revision,baseline]]
    project,_,media,_=reopen_project(directory)
    archived=load_archive(project,revision)
    base_manifest=read_json(baseline/"export_manifest.json")
    verify_source(base_manifest,project,"subtitle_sha256")
    verify_bundle(baseline,base_manifest,"artifact_hashes")
    known=(base_manifest.get("revision_id")==archived["baseline_fusion_revision_id"] and
           sha256(baseline/"export_manifest.json")==archived["baseline_manifest_sha256"] and
           sha256(baseline/"track.funscript")==archived["baseline_script_sha256"])
    profile=DiffProfile.model_validate_json(json.dumps(yaml.safe_load(Path(profile_path).read_text(encoding="utf-8-sig"))))
    base_raw=(revision/"baseline.funscript").read_bytes()
    external_raw=(revision/"external_original.funscript").read_bytes()
    duration=media["duration_ms"]
    report=compare_scripts(base_raw,external_raw,duration,project.config.device,profile,known)
    report=inherited_protection(report,archived,duration)
    revision_id=str(uuid.uuid4())
    with stage_output(output,[directory,revision,baseline]) as stage:
        (stage/"baseline.funscript").write_bytes(base_raw)
        (stage/"external_original.funscript").write_bytes(external_raw)
        atomic_json(stage/"source_archive_manifest.json",archived)
        atomic_json(stage/"comparison_profile.json",profile.model_dump(mode="json"))
        atomic_json(stage/"diff_report.json",report)
        if known and "segment_plan.json" in base_manifest["artifact_hashes"]:
            shutil.copyfile(baseline/"segment_plan.json",stage/"baseline_plan_context.json")
        draw_comparison(stage/"diff_preview.png",decoded(base_raw,duration,project.config.device),
            decoded(external_raw,duration,project.config.device),duration,report["protected_ranges"],revision_id,report["classification"])
        return write_manifest(stage,"diff_manifest.json",{"schema_version":"1.3","revision_id":revision_id,
            "status":"needs_review","project_id":project.project_id,"source_revision_id":project.revision_id,
            "subtitle_sha256":project.artifact_hashes["subtitles"],"original_audio_sha256":project.input_hashes["audio"],
            "external_revision_id":archived["revision_id"],"archive_manifest_sha256":sha256(revision/"revision_manifest.json"),
            "baseline_known":known,"classification":report["classification"],"human_intent_inferred":False,
            "automatic_merge_allowed":report["automatic_merge_allowed"]})


def overlaps(a,b):
    return a["start_ms"]<b["end_ms"] and b["start_ms"]<a["end_ms"]


def prepare_regeneration(directory,diff_directory,profile_path,output):
    directory,diff_directory=Path(directory).resolve(),Path(diff_directory).resolve()
    project,_,media,_=reopen_project(directory)
    diff_manifest=load_packet(project,diff_directory,"diff_manifest.json")
    comparison=DiffProfile.model_validate_json((diff_directory/"comparison_profile.json").read_text(encoding="utf-8"))
    profile=RegenerationProfile.model_validate_json(json.dumps(yaml.safe_load(Path(profile_path).read_text(encoding="utf-8-sig"))))
    base_raw=(diff_directory/"baseline.funscript").read_bytes()
    original_raw=(diff_directory/"external_original.funscript").read_bytes()
    duration=media["duration_ms"]
    # Recompute protection from the immutable original, never trust edited range lists.
    report=compare_scripts(base_raw,original_raw,duration,project.config.device,comparison,diff_manifest["baseline_known"])
    report=inherited_protection(report,read_json(diff_directory/"source_archive_manifest.json"),duration)
    profile=profile.model_copy(update={"locked_ranges":profile.locked_ranges+project_locks(directory,project,duration)})
    original=decoded(original_raw,duration,project.config.device)
    baseline=decoded(base_raw,duration,project.config.device)
    windows=[window.model_dump() for window in sorted(profile.windows,key=lambda item:item.start_ms)]
    locks=[item.model_dump() for item in profile.locked_ranges]
    conflicts=[]
    if not report["automatic_merge_allowed"] or original is None:
        conflicts.append({"reason":"comparison_does_not_confirm_unaffected_ranges"})
    times={point["at"] for point in original["actions"]} if original else set()
    for lock in locks:
        if lock["end_ms"]>duration:
            raise ValueError("lock extends beyond media duration")
    for window in windows:
        if window["end_ms"]>duration:
            raise ValueError("regeneration window extends beyond media duration")
        if window["start_ms"] not in times or window["end_ms"] not in times:
            conflicts.append({"window_id":window["window_id"],"reason":"window_endpoints_must_be_existing_external_points"})
        for protected in report["protected_ranges"]+locks:
            if overlaps(window,protected):
                conflicts.append({"window_id":window["window_id"],"reason":"window_overlaps_protection_or_lock","range":protected})
    candidate=None
    validation={"format_valid":False,"constraint_errors":[],"device_constraints_passed":False}
    proof={"outside_points_unchanged":False,"outside_curve_unchanged":False,"protected_and_locked_curves_unchanged":False}
    if not conflicts:
        candidate=deepcopy(original)
        old_points=original["actions"]
        lookup={point["at"]:point for point in old_points}
        inside=lambda time:any(window["start_ms"]<time<window["end_ms"] for window in windows)
        points=[deepcopy(point) for point in old_points if not inside(point["at"])]
        for window in windows:
            start,end=window["start_ms"],window["end_ms"]
            length=end-start
            config=project.config.model_copy(deep=True)
            config.generation=GenerationConfig(initial_position=lookup[start]["pos"],presets=profile.presets,
                segments=[Segment(segment_id=window["window_id"],start_ms=0,end_ms=length-window["end_transition_ms"],preset_id=window["preset_id"])])
            local,_=generate(config,length,allow_constraint_errors=True)
            local["actions"][-1]["pos"]=lookup[end]["pos"]
            points.extend({"at":point["at"]+start,"pos":point["pos"]} for point in local["actions"] if 0<point["at"]<length)
        candidate["actions"]=sorted(points,key=lambda point:point["at"])
        outside=complement([(window["start_ms"],window["end_ms"]) for window in windows],duration)
        protected=[(item["start_ms"],item["end_ms"]) for item in report["protected_ranges"]+locks]
        proof={"outside_points_unchanged":[point for point in old_points if not inside(point["at"])]==[point for point in candidate["actions"] if not inside(point["at"])],
               "outside_curve_unchanged":equal_on_ranges(old_points,candidate["actions"],outside),
               "protected_and_locked_curves_unchanged":equal_on_ranges(old_points,candidate["actions"],protected)}
        if not all(proof.values()):
            conflicts.append({"reason":"curve_preservation_proof_failed"})
        validation=validate_actions(candidate["actions"],duration,project.config.device)
        if validation["constraint_errors"]:
            conflicts.append({"reason":"final_candidate_constraints_failed","errors":validation["constraint_errors"]})
    candidate_id=str(uuid.uuid4())
    with stage_output(output,[directory,diff_directory]) as stage:
        (stage/"parent_external_original.funscript").write_bytes(original_raw)
        (stage/"baseline.funscript").write_bytes(base_raw)
        atomic_json(stage/"source_diff_manifest.json",diff_manifest)
        atomic_json(stage/"regeneration_profile.json",profile.model_dump(mode="json"))
        atomic_json(stage/"diff_report.json",report)
        atomic_json(stage/"validation.json",validation)
        atomic_json(stage/"preservation_proof.json",proof)
        if candidate:
            atomic_json(stage/"candidate_curve.json",candidate)
        atomic_json(stage/"conflicts.json",conflicts)
        draw_comparison(stage/"candidate_preview.png",baseline,original,duration,report["protected_ranges"],candidate_id,
                        "BLOCKED" if conflicts else "CANDIDATE - not yet accepted",candidate,locks)
        return write_manifest(stage,"candidate_manifest.json",{"schema_version":"1.3","candidate_id":candidate_id,
            "status":"failed" if conflicts else "needs_review","eligible_for_acceptance":not conflicts,
            "project_id":project.project_id,"source_revision_id":project.revision_id,"subtitle_sha256":project.artifact_hashes["subtitles"],
            "original_audio_sha256":project.input_hashes["audio"],"external_revision_id":diff_manifest["external_revision_id"],
            "source_diff_manifest_sha256":sha256(diff_directory/"diff_manifest.json"),"conflict_count":len(conflicts),
            "diagnostic_curve_status":"old_diagnostics_are_source_context_only","source_interpolation_confirmed":comparison.external_interpolation_confirmed,
            "source_external_sha256":sha256(diff_directory/"external_original.funscript")})


def accept_candidate(directory,candidate_directory,candidate_id,output):
    directory,candidate_directory=Path(directory).resolve(),Path(candidate_directory).resolve()
    project,_,media,_=reopen_project(directory)
    manifest=load_packet(project,candidate_directory,"candidate_manifest.json")
    if candidate_id!=manifest.get("candidate_id") or not manifest.get("eligible_for_acceptance"):
        raise ValueError("candidate ID mismatch or proposal blocked")
    if not all(read_json(candidate_directory/"preservation_proof.json").values()):
        raise ValueError("candidate preservation proof failed")
    candidate=read_json(candidate_directory/"candidate_curve.json")
    original=read_json(candidate_directory/"parent_external_original.funscript")
    profile=RegenerationProfile.model_validate_json((candidate_directory/"regeneration_profile.json").read_text(encoding="utf-8"))
    ranges=[(item.start_ms,item.end_ms) for item in profile.windows]
    outside=complement(ranges,media["duration_ms"])
    diff=read_json(candidate_directory/"diff_report.json")
    protected=[(item["start_ms"],item["end_ms"]) for item in diff["protected_ranges"]]+[(item.start_ms,item.end_ms) for item in profile.locked_ranges]
    live_locks=project_locks(directory,project,media["duration_ms"])
    if any(overlaps(window.model_dump(),lock.model_dump()) for window in profile.windows for lock in live_locks):
        raise ValueError("candidate intersects a current project lock")
    inside=lambda time:any(start<time<end for start,end in ranges)
    if ([point for point in original["actions"] if not inside(point["at"])] != [point for point in candidate["actions"] if not inside(point["at"])]
            or {key:value for key,value in original.items() if key!="actions"} != {key:value for key,value in candidate.items() if key!="actions"}):
        raise ValueError("candidate changed outside points or format fields")
    if not equal_on_ranges(original["actions"],candidate["actions"],outside+protected):
        raise ValueError("candidate no longer preserves outside/protected curves")
    validation=validate_actions(candidate["actions"],media["duration_ms"],project.config.device)
    if validation["constraint_errors"]:
        raise ValueError("candidate failed final constraints")
    revision_id=str(uuid.uuid4())
    with stage_output(output,[directory,candidate_directory]) as stage:
        shutil.copyfile(candidate_directory/"candidate_curve.json",stage/"track.funscript")
        shutil.copyfile(candidate_directory/"parent_external_original.funscript",stage/"parent_external_original.funscript")
        atomic_json(stage/"validation.json",validation)
        atomic_json(stage/"regeneration_profile.json",profile.model_dump(mode="json"))
        atomic_json(stage/"source_candidate_manifest.json",manifest)
        draw_comparison(stage/"preview.png",original,original,media["duration_ms"],diff["protected_ranges"],revision_id,"ACCEPTED CHILD REVISION",candidate,[item.model_dump() for item in profile.locked_ranges])
        return write_manifest(stage,"export_manifest.json",{"schema_version":"1.3","revision_id":revision_id,"status":"needs_review",
            "operation":"accepted_local_regeneration","script_exported":True,"project_id":project.project_id,"source_revision_id":project.revision_id,
            "subtitle_sha256":project.artifact_hashes["subtitles"],"audio_sha256":project.input_hashes["audio"],"candidate_id":candidate_id,
            "source_candidate_manifest_sha256":sha256(candidate_directory/"candidate_manifest.json"),"parent_external_sha256":manifest["source_external_sha256"],
            "parent_external_revision_id":manifest["external_revision_id"],
            "protected_ranges":diff["protected_ranges"],"locked_ranges":[item.model_dump() for item in profile.locked_ranges],"plan_context_status":"not_a_fused_semantic_plan",
            "diagnostic_curve_status":"old_diagnostics_are_source_context_only","human_review_required":True})


def restore_revision(directory,applied_directory,target,output):
    directory,applied_directory=Path(directory).resolve(),Path(applied_directory).resolve()
    project,_,media,_=reopen_project(directory)
    manifest=load_packet(project,applied_directory,"export_manifest.json")
    if target not in ("parent","result") or "parent_external_original.funscript" not in manifest["artifact_hashes"]:
        raise ValueError("restoration requires an applied regeneration and target parent/result")
    source=applied_directory/("parent_external_original.funscript" if target=="parent" else "track.funscript")
    raw=source.read_bytes()
    validation,data=validate_external(raw,media["duration_ms"],project.config.device)
    revision_id=str(uuid.uuid4())
    with stage_output(output,[directory,applied_directory]) as stage:
        export_ok=validation["format_valid"] and not validation.get("errors",[])
        (stage/("track.funscript" if export_ok else "restored_original.funscript")).write_bytes(raw)
        atomic_json(stage/"validation.json",validation)
        return write_manifest(stage,"export_manifest.json",{"schema_version":"1.3","revision_id":revision_id,
            "status":"needs_review" if export_ok else "failed","operation":"restore_"+target,"script_exported":export_ok,"project_id":project.project_id,
            "source_revision_id":project.revision_id,"subtitle_sha256":project.artifact_hashes["subtitles"],"audio_sha256":project.input_hashes["audio"],
            "protected_ranges":manifest.get("protected_ranges",[]),"locked_ranges":manifest.get("locked_ranges",[]),
            "restored_from_manifest_sha256":sha256(applied_directory/"export_manifest.json"),"restored_bytes_sha256":sha256(source),
            "diagnostic_curve_status":"old_diagnostics_are_source_context_only","human_review_required":True})
