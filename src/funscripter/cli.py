import argparse
import json
import sys
from types import SimpleNamespace
from .project import create_project, reopen_project, analyze_project, plan_project


def execute_stage(command, arguments):
    args = SimpleNamespace(command=command, **arguments)
    if command == "create":
        project = create_project(args.config, args.output)
        return {"project_id": project.project_id, "status": project.status,
                "output": args.output, "script": project.artifacts["script"]}
    if command == "diff-revision":
        from .revision_workflow import diff_revision
        return diff_revision(args.project, args.revision, args.baseline, args.profile, args.output)
    if command == "prepare-regeneration":
        from .revision_workflow import prepare_regeneration
        return prepare_regeneration(args.project, args.diff, args.profile, args.output)
    if command == "accept-candidate":
        from .revision_workflow import accept_candidate
        return accept_candidate(args.project, args.candidate, args.candidate_id, args.output)
    if command == "restore-revision":
        from .revision_workflow import restore_revision
        return restore_revision(args.project, args.applied, args.target, args.output)
    if command == "diagnostic":
        from .diagnostic import create_diagnostic
        return create_diagnostic(args.project, args.draft, args.output, args.profile)
    if command == "archive-revision":
        from .revisions import archive_revision
        return archive_revision(args.project, args.baseline, args.script, args.output,
                                args.diagnostic, args.elapsed_seconds, args.notes)
    if command == "fuse":
        from .fuse_project import fuse_project
        return fuse_project(args.project, args.planning, args.output, args.profile, args.analysis, args.boundaries)
    if command == "plan":
        return plan_project(args.project, args.output, args.profile, args.resume)
    if command == "analyze":
        return analyze_project(args.project, args.output, args.profile, args.reuse_analysis)
    if command == "multiaxis-draft":
        from .multiaxis import export_multiaxis_draft
        return export_multiaxis_draft(args.project, args.profile, args.output)
    if command == "utterance-candidates":
        from .boundaries import create_boundary_candidates
        return create_boundary_candidates(args.project, args.analysis, args.output, args.profile, args.vad_evidence)
    raise ValueError("unsupported stage command")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local single-axis draft workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a new immutable draft project")
    create.add_argument("--config", required=True)
    create.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect", help="reopen and verify saved project artifacts")
    inspect.add_argument("project")
    transcribe = commands.add_parser("transcribe", help="transcribe audio using a local faster-whisper model")
    transcribe.add_argument("audio")
    transcribe.add_argument("--profile", required=True)
    transcribe.add_argument("--output", required=True)
    transcribe.add_argument("--ffmpeg", default="ffmpeg")
    transcribe.add_argument("--ffprobe", default="ffprobe")
    transcribe.add_argument("--audio-stream", type=int, default=0)
    preview = commands.add_parser("preview", help="build an offline interactive audio/script preview")
    preview.add_argument("project")
    preview.add_argument("--draft", required=True)
    preview.add_argument("--planning", help="optional matching planning bundle for original model explanations")
    preview.add_argument("--output", required=True)
    analyze = commands.add_parser("analyze", help="extract acoustic evidence without changing the project")
    analyze.add_argument("project")
    analyze.add_argument("--output", required=True)
    analyze.add_argument("--profile", help="optional standalone audio analysis YAML profile")
    analyze.add_argument("--reuse-analysis", help="verify and reuse acoustics from an existing analysis bundle")
    plan = commands.add_parser("plan", help="run two-pass subtitle planning against a configured service")
    plan.add_argument("project")
    plan.add_argument("--output", required=True)
    plan.add_argument("--profile", help="standalone LLM YAML profile")
    plan.add_argument("--resume", action="store_true", help="reuse accepted requests from the same run")
    fuse = commands.add_parser("fuse", help="fuse validated semantics/audio into a reviewable single-axis draft")
    fuse.add_argument("project")
    fuse.add_argument("--planning", required=True)
    fuse.add_argument("--analysis")
    fuse.add_argument("--boundaries", help="optional verified review-only utterance candidate bundle")
    fuse.add_argument("--profile", required=True)
    fuse.add_argument("--output", required=True)
    diagnostic = commands.add_parser("diagnostic", help="render a revision-bound MP4 and matching script")
    diagnostic.add_argument("project")
    diagnostic.add_argument("--draft", required=True)
    diagnostic.add_argument("--profile", required=True)
    diagnostic.add_argument("--output", required=True)
    archive = commands.add_parser("archive-revision", help="preserve a complete OFS revision with its baseline")
    archive.add_argument("project")
    archive.add_argument("--baseline", required=True)
    archive.add_argument("--script", required=True)
    archive.add_argument("--diagnostic")
    archive.add_argument("--output", required=True)
    archive.add_argument("--elapsed-seconds", type=float)
    archive.add_argument("--notes", default="")
    diff = commands.add_parser("diff-revision", help="compare an archived external revision to its exact baseline")
    diff.add_argument("project")
    diff.add_argument("--revision", required=True)
    diff.add_argument("--baseline", required=True)
    diff.add_argument("--profile", required=True)
    diff.add_argument("--output", required=True)
    regenerate = commands.add_parser("prepare-regeneration", help="prepare a protected, immutable local candidate")
    regenerate.add_argument("project")
    regenerate.add_argument("--diff", required=True)
    regenerate.add_argument("--profile", required=True)
    regenerate.add_argument("--output", required=True)
    accept = commands.add_parser("accept-candidate", help="publish the exact reviewed candidate as a new child revision")
    accept.add_argument("project")
    accept.add_argument("--candidate", required=True)
    accept.add_argument("--candidate-id", required=True)
    accept.add_argument("--output", required=True)
    restore = commands.add_parser("restore-revision", help="restore the parent or result of an applied regeneration")
    restore.add_argument("project")
    restore.add_argument("--applied", required=True)
    restore.add_argument("--target", choices=["parent","result"], required=True)
    restore.add_argument("--output", required=True)
    multiaxis = commands.add_parser("multiaxis-draft", help="export an offline numeric axis-contract fixture")
    multiaxis.add_argument("project")
    multiaxis.add_argument("--profile", required=True)
    multiaxis.add_argument("--output", required=True)
    utterance = commands.add_parser("utterance-candidates", help="extract review-only audio end candidates")
    utterance.add_argument("project")
    utterance.add_argument("--analysis", required=True)
    utterance.add_argument("--profile")
    utterance.add_argument("--vad-evidence", help="optional precomputed VAD evidence JSON; no model is run")
    utterance.add_argument("--output", required=True)
    task_status = commands.add_parser("task-status", help="read a persistent task record")
    task_status.add_argument("task_file")
    task_resume = commands.add_parser("task-resume", help="verify and recover a recorded task")
    task_resume.add_argument("task_file")
    batch = commands.add_parser("batch", help="run a YAML batch serially with persistent child tasks")
    batch.add_argument("--config", required=True)
    batch.add_argument("--output", required=True, help="new batch directory; contains .batch metadata")
    batch_status = commands.add_parser("batch-status", help="read saved batch progress")
    batch_status.add_argument("directory")
    batch_resume = commands.add_parser("batch-resume", help="verify and resume a serial batch")
    batch_resume.add_argument("directory")
    for stage in (create, analyze, plan, fuse, diagnostic, archive, diff, regenerate, accept, restore, multiaxis, utterance):
        stage.add_argument("--task-file", help="optional task JSON outside inputs and output; default beside output")
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            from .interactive_preview import create_interactive_preview
            result = create_interactive_preview(args.project, args.draft, args.output, args.planning)
        elif args.command == "transcribe":
            from .transcription import transcribe_audio
            result = transcribe_audio(args.audio, args.output, args.profile, args.ffmpeg,
                                      args.ffprobe, args.audio_stream)
        elif args.command == "batch":
            from .batch import run_batch
            result = run_batch(args.config, args.output, execute_stage)
        elif args.command == "batch-status":
            from .batch import batch_status
            result = batch_status(args.directory)
        elif args.command == "batch-resume":
            from .batch import resume_batch
            result = resume_batch(args.directory, execute_stage)
        elif args.command == "task-status":
            from .tasks import task_summary
            result = task_summary(args.task_file)
        elif args.command == "task-resume":
            from .tasks import resume_task
            result = resume_task(args.task_file, execute_stage)
        elif args.command == "inspect":
            project, timeline, media, validation = reopen_project(args.project)
            result = {"project_id": project.project_id, "status": project.status,
                      "duration_ms": media["duration_ms"], "cue_count": len(timeline.cues),
                      "issues": timeline.issues, "validation": validation,
                      "external_tools": project.external_tools}
        else:
            from .tasks import run_task, FIELDS
            arguments = {key: getattr(args, key) for key in FIELDS[args.command]}
            result = run_task(args.command, arguments, execute_stage, args.task_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        status = result.get("execution_status", result.get("status"))
        # Querying a failed record succeeds; running/recovering that task reports failure.
        if args.command in ("task-status", "batch-status"):
            return 0
        return 130 if status == "cancelled" else (2 if status == "failed" else 0)
    except (OSError, ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
