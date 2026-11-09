import argparse
import json
import sys
from .project import create_project, reopen_project, analyze_project, plan_project


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local single-axis draft workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a new immutable draft project")
    create.add_argument("--config", required=True)
    create.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect", help="reopen and verify saved project artifacts")
    inspect.add_argument("project")
    analyze = commands.add_parser("analyze", help="extract acoustic evidence without changing the project")
    analyze.add_argument("project")
    analyze.add_argument("--output", required=True)
    analyze.add_argument("--profile", help="optional standalone audio analysis YAML profile")
    plan = commands.add_parser("plan", help="run two-pass subtitle planning against a configured service")
    plan.add_argument("project")
    plan.add_argument("--output", required=True)
    plan.add_argument("--profile", help="standalone LLM YAML profile")
    plan.add_argument("--resume", action="store_true", help="reuse accepted requests from the same run")
    fuse = commands.add_parser("fuse", help="fuse validated semantics/audio into a reviewable single-axis draft")
    fuse.add_argument("project")
    fuse.add_argument("--planning", required=True)
    fuse.add_argument("--analysis")
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
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            project = create_project(args.config, args.output)
            result = {"project_id": project.project_id, "status": project.status,
                      "output": args.output, "script": project.artifacts["script"]}
        elif args.command == "diff-revision":
            from .revision_workflow import diff_revision
            result=diff_revision(args.project,args.revision,args.baseline,args.profile,args.output)
        elif args.command == "prepare-regeneration":
            from .revision_workflow import prepare_regeneration
            result=prepare_regeneration(args.project,args.diff,args.profile,args.output)
        elif args.command == "accept-candidate":
            from .revision_workflow import accept_candidate
            result=accept_candidate(args.project,args.candidate,args.candidate_id,args.output)
        elif args.command == "restore-revision":
            from .revision_workflow import restore_revision
            result=restore_revision(args.project,args.applied,args.target,args.output)
        elif args.command == "diagnostic":
            from .diagnostic import create_diagnostic
            result = create_diagnostic(args.project,args.draft,args.output,args.profile)
        elif args.command == "archive-revision":
            from .revisions import archive_revision
            result = archive_revision(args.project,args.baseline,args.script,args.output,args.diagnostic,args.elapsed_seconds,args.notes)
        elif args.command == "fuse":
            from .fuse_project import fuse_project
            result = fuse_project(args.project, args.planning, args.output, args.profile, args.analysis)
        elif args.command == "plan":
            result = plan_project(args.project, args.output, args.profile, args.resume)
        elif args.command == "analyze":
            result = analyze_project(args.project, args.output, args.profile)
        else:
            project, timeline, media, validation = reopen_project(args.project)
            result = {"project_id": project.project_id, "status": project.status,
                      "duration_ms": media["duration_ms"], "cue_count": len(timeline.cues),
                      "issues": timeline.issues, "validation": validation,
                      "external_tools": project.external_tools}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 130 if result.get("status") == "cancelled" else (2 if result.get("status") == "failed" else 0)
    except (OSError, ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
