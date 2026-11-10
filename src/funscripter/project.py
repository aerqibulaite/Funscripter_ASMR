import csv
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
import yaml
from .models import Config, Project, Timeline
from .media import decode_audio, sha256
from .lrc import candidate_document
from .subtitles import parse_subtitles, subtitle_format
from .motion import generate, validate_actions


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    # JSON validation accepts YAML arrays for tuple fields, still rejects coerced scalars.
    config = Config.model_validate_json(json.dumps(raw, allow_nan=False))
    if config.llm:
        from .model_profiles import resolve_knowledge
        config.llm = resolve_knowledge(config.llm, path.parent)
    for value in [config.audio, config.subtitles.path]:
        if not (path.parent / value).is_file():
            raise ValueError(f"input not found: {value}")
    return config, path.parent


def create_project(config_path, output):
    config, base = load_config(config_path)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("project output already exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-", dir=output.parent))
    try:
        (stage / "media").mkdir()
        (stage / "inputs").mkdir()
        (stage / "exports").mkdir()
        source = (base / config.audio).resolve()
        subtitle = (base / config.subtitles.path).resolve()
        media = decode_audio(source, stage / "media/analysis.wav", config.ffmpeg, config.ffprobe, config.audio_stream)
        duration = media["duration_ms"]
        if duration < 1:
            raise ValueError("audio must be at least one millisecond")
        subtitle_copy = "inputs/subtitles." + subtitle_format(config.subtitles)
        shutil.copyfile(subtitle, stage / subtitle_copy)
        timeline = parse_subtitles(subtitle.read_text(encoding=config.subtitles.encoding), duration, config.subtitles)
        script, validation = generate(config, duration)
        artifacts = {"media": "media_manifest.json", "subtitles": "subtitles.json",
            "candidates": "utterance_candidates.json", "base": "generated_base.json",
            "overrides": "manual_overrides.json", "script": "exports/track.funscript",
            "validation": "exports/validation.json", "review": "exports/review.csv",
            "config": "config.resolved.yaml", "subtitle_source": subtitle_copy,
            "analysis_audio": "media/analysis.wav"}
        values = {"media": media, "subtitles": timeline.model_dump(),
            "candidates": candidate_document(timeline), "base": script,
            "overrides": {"schema_version": "1.3", "overrides": [], "locked_ranges": []},
            "script": script, "validation": validation}
        for key, value in values.items():
            atomic_json(stage / artifacts[key], value)
        (stage / artifacts["config"]).write_text(yaml.safe_dump(config.model_dump(mode="json"),
            allow_unicode=True, sort_keys=False), encoding="utf-8")
        with (stage / artifacts["review"]).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cue_id", "start_ms", "end_ms", "reason", "text"])
            for cue in timeline.cues:
                reason = "inferred_segment_end_not_utterance_end" if cue.end_inferred else "subtitle_display_end_not_utterance_end"
                writer.writerow([cue.cue_id, cue.start_ms, cue.end_ms, reason, cue.text_raw])
            for issue in timeline.issues:
                writer.writerow(["", issue.get("time_ms", ""), "", issue["reason"], ""])
            if media.get("source_kind") == "video":
                writer.writerow(["", 0, duration, "video_container_timeline_requires_sync_review", ""])
            writer.writerow(["", 0, duration, "manual_presets_only_no_semantic_or_audio_analysis", ""])
        project = Project(project_id=str(uuid.uuid4()), revision_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(), config=config,
            input_hashes={"audio": media["source_sha256"], "subtitles": sha256(subtitle)},
            artifacts=artifacts, artifact_hashes={key: sha256(stage / relative)
                for key, relative in artifacts.items()},
            external_tools={"editor": "OpenFunscripter (OFS): not verified",
                            "player": "MultiFunPlayer: not verified"})
        atomic_json(stage / "project.json", project.model_dump(mode="json"))
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return project


def reopen_project(directory):
    directory = Path(directory).resolve()
    project = Project.model_validate_json((directory / "project.json").read_text(encoding="utf-8"))
    for key, relative in project.artifacts.items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("artifact path escapes project directory")
        if sha256(path) != project.artifact_hashes.get(key):
            raise ValueError(f"artifact changed: {key}; keep external edits as a separate revision")
    timeline = Timeline.model_validate_json((directory / project.artifacts["subtitles"]).read_text(encoding="utf-8"))
    media = json.loads((directory / project.artifacts["media"]).read_text(encoding="utf-8"))
    script = json.loads((directory / project.artifacts["script"]).read_text(encoding="utf-8"))
    report = validate_actions(script["actions"], media["duration_ms"], project.config.device)
    if report["constraint_errors"]:
        raise ValueError("saved script failed validation")
    return project, timeline, media, report


def analyze_project(directory, output, profile_path=None, reuse_analysis=None):
    from .audio import extract_audio
    from .audio_cache import reuse_acoustics
    from . import audio_cache
    from .models import AudioAnalysisConfig
    directory = Path(directory).resolve()
    project, timeline, media, _ = reopen_project(directory)
    profile = project.config.audio_analysis
    if profile_path is not None:
        raw = yaml.safe_load(Path(profile_path).read_text(encoding="utf-8-sig"))
        profile = AudioAnalysisConfig.model_validate_json(json.dumps(raw, allow_nan=False))
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("analysis output already exists; choose a new directory")
    if output.is_relative_to(directory):
        raise ValueError("analysis output must be outside the source project")
    reuse_analysis = Path(reuse_analysis).resolve() if reuse_analysis else None
    if reuse_analysis and output.is_relative_to(reuse_analysis):
        raise ValueError("analysis output must be outside the reused analysis")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-analysis-", dir=output.parent))
    try:
        evidence, cache = None, {"status": "not_requested", "reasons": ["no_reuse_source"]}
        if reuse_analysis:
            evidence, cache = reuse_acoustics(reuse_analysis, stage, profile,
                project.artifact_hashes["analysis_audio"], media, timeline)
        if evidence is None:
            evidence = extract_audio(directory / project.artifacts["analysis_audio"], stage, profile,
                project.artifact_hashes["analysis_audio"], timeline)
        evidence["project_id"] = project.project_id
        evidence["source_revision_id"] = project.revision_id
        evidence["subtitle_sha256"] = project.artifact_hashes["subtitles"]
        atomic_json(stage / "audio_evidence.json", evidence)
        # Export compact acoustic rows independently of manual generation review.csv.
        with (stage / "audio_summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cue_id", "start_ms", "end_ms", "status", "mean_rms", "peak_rms"])
            for row in evidence["cue_summaries"]:
                writer.writerow([row[key] for key in ["cue_id", "start_ms", "end_ms", "status", "mean_rms", "peak_rms"]])
        manifest = {"schema_version": "1.3", "analysis_id": str(uuid.uuid4()),
            "project_id": project.project_id, "source_revision_id": project.revision_id,
            "status": evidence["status"], "acoustic_cache_key": evidence["cache_key"],
            "cache": cache, "summary_implementation_sha256": sha256(audio_cache.__file__),
            "summary_subtitle_sha256": project.artifact_hashes["subtitles"],
            "artifacts": {name: sha256(stage / name) for name in
                ["audio_features.npz", "audio_evidence.json", "audio_summary.csv"]}}
        atomic_json(stage / "analysis_manifest.json", manifest)
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return manifest


def plan_project(directory, output, profile_path=None, resume=False, transport=None):
    from .models import LLMConfig
    from .planner import Planner, PROMPT_VERSION, digest
    from .planning_inputs import planning_inputs
    directory = Path(directory).resolve()
    project, timeline, media, _ = reopen_project(directory)
    config = project.config.llm
    if profile_path is not None:
        from .model_profiles import load_model_profile
        config = load_model_profile(profile_path)
    if config is None:
        raise ValueError("configure llm or provide --profile; no default remote service")
    if not timeline.cues:
        raise ValueError("planning requires at least one nonempty subtitle cue")
    output = Path(output).resolve()
    if output.is_relative_to(directory):
        raise ValueError("planning output must be outside the source project")
    planner = Planner(config, output, transport)
    eligible, input_view = planning_inputs(timeline, media["duration_ms"])
    planner.quality_flags = {row["cue_id"]: row["quality_flags"] for row in input_view["cues"]}
    planner.assessments = [{"cue_id": row["cue_id"], "status": "excluded", "reason": "; ".join(row["reasons"])}
                           for row in input_view["cues"] if row["status"] == "excluded"]
    signature = digest({"project_id": project.project_id, "revision_id": project.revision_id,
        "subtitles": project.artifact_hashes["subtitles"], "config": config.model_dump(mode="json"),
        "implementation": planner.implementation_hash, "prompt_version": PROMPT_VERSION,
        "knowledge_sha256": planner.knowledge_hash})
    run_path = output / "planning_run.json"
    if output.exists():
        if not resume:
            raise ValueError("planning output already exists; use --resume for the same run")
        old = json.loads(run_path.read_text(encoding="utf-8"))
        if old.get("signature") != signature:
            raise ValueError("resume source/config/code mismatch; choose a new output directory")
    else:
        if resume:
            raise ValueError("cannot resume a missing planning directory")
        output.mkdir(parents=True)
    run = {"schema_version": "1.3", "signature": signature, "project_id": project.project_id,
        "source_revision_id": project.revision_id, "subtitle_sha256": project.artifact_hashes["subtitles"],
        "config": config.model_dump(mode="json"), "prompt_version": PROMPT_VERSION,
        "implementation_sha256": planner.implementation_hash,
        "knowledge_sha256": planner.knowledge_hash,
        "token_budget_method": planner.budget_method,
        "status": "running", "human_review_required": True,
        "semantic_schema_version": "1.4", "eligible_cue_count": len(eligible),
        "excluded_cue_count": input_view["excluded_count"]}
    atomic_json(output / "planning_inputs.json", input_view)
    atomic_json(output / "semantic_knowledge.json", {"source": config.semantic_knowledge_file,
                "sha256": planner.knowledge_hash, "content": planner.knowledge})
    atomic_json(run_path, run)
    events, failures = [], []
    outline = None
    try:
        outline, strategy = planner.outline(eligible) if eligible else (None, "no_eligible_cues")
        run["global_input_strategy"] = strategy
        if outline is None:
            failures = [{"cue_id": cue.cue_id, "reason": "global_outline_failed"} for cue in eligible]
        else:
            atomic_json(output / "outline.json", outline.model_dump(mode="json"))
            events, failures = planner.semantic(eligible, outline)
        run["status"] = "failed" if failures else "needs_review"
    except ValueError as exc:
        run.update(status="failed", error=str(exc)[:500])
        if (output / "semantic_events.json").exists():
            checkpoint = json.loads((output / "semantic_events.json").read_text(encoding="utf-8"))
            events = checkpoint["events"]
            planner.assessments = checkpoint["cue_assessments"]
            planner.context_notes = checkpoint.get("context_notes", [])
        covered = {row["cue_id"] for row in planner.assessments if row["status"] != "failed"}
        failures = [{"cue_id": cue.cue_id, "reason": "planning_failed"} for cue in eligible if cue.cue_id not in covered]
    except KeyboardInterrupt:
        run["status"] = "cancelled"
        # Keep any completed semantic checkpoint instead of replacing it with an empty result.
        if (output / "semantic_events.json").exists():
            checkpoint = json.loads((output / "semantic_events.json").read_text(encoding="utf-8"))
            events, failures = checkpoint["events"], checkpoint["failures"]
            planner.assessments = checkpoint["cue_assessments"]
            planner.context_notes = checkpoint.get("context_notes", [])
        covered = {row["cue_id"] for row in planner.assessments} | {row["cue_id"] for row in failures}
        failures.extend({"cue_id": cue.cue_id, "reason": "cancelled"} for cue in eligible if cue.cue_id not in covered)
    assessments = {row["cue_id"]: row for row in planner.assessments}
    for failure in failures:
        assessments[failure["cue_id"]] = {"cue_id": failure["cue_id"], "status": "failed", "reason": failure["reason"]}
    planner.assessments = [assessments[cue.cue_id] for cue in timeline.cues]
    atomic_json(output / "semantic_events.json", planner.semantic_document(events, failures))
    atomic_json(output / "request_log.json", planner.trace)
    run["event_count"], run["failure_count"] = len(events), len(failures)
    run["artifact_hashes"] = {name: sha256(output / name) for name in
        ["outline.json", "semantic_events.json", "request_log.json", "planning_inputs.json", "semantic_knowledge.json"] if (output / name).exists()}
    atomic_json(run_path, run)
    return run
