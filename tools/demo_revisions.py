"""Scripted M5 revision demo; no real OFS edits or editor calibration are claimed."""
import argparse
import json
import sys
from pathlib import Path
import yaml
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/"src"))
from funscripter.revisions import archive_revision
from funscripter.revision_workflow import diff_revision,prepare_regeneration,accept_candidate,restore_revision


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",default=str(root/"workspace/m3-demo-final/project"))
    parser.add_argument("--baseline",default=str(root/"workspace/m3-demo-final/fusion"))
    parser.add_argument("--output",default=str(root/"workspace/m5-demo"))
    args=parser.parse_args()
    target=Path(args.output).resolve()
    if target.exists():
        parser.error("output already exists; choose a new directory")
    target.mkdir(parents=True)
    baseline=Path(args.baseline).resolve()
    data=json.loads((baseline/"track.funscript").read_text(encoding="utf-8-sig"))
    point=next(point for point in data["actions"] if point["at"]==2000)
    point["pos"]+=2
    edited=target/"scripted-ofs-edit.funscript"
    edited.write_bytes(b"\xef\xbb\xbf"+json.dumps(data,ensure_ascii=False).encode())
    archive_revision(args.project,baseline,edited,target/"archive",notes="Scripted fixture; not a real OFS session")
    diff_profile={"interpolation":"linear","external_interpolation_confirmed":True,
        "comparison_reference":"SCRIPTED LINEAR FIXTURE ONLY; not real editor calibration","time_tolerance_ms":0}
    (target/"diff-profile.yaml").write_text(yaml.safe_dump(diff_profile),encoding="utf-8")
    diff_revision(args.project,target/"archive",baseline,target/"diff-profile.yaml",target/"diff")
    proposal=prepare_regeneration(args.project,target/"diff",root/"config/regeneration-example.yaml",target/"proposal")
    if not proposal["eligible_for_acceptance"]:
        raise ValueError("demo proposal was unexpectedly blocked; inspect conflicts.json")
    accepted=accept_candidate(args.project,target/"proposal",proposal["candidate_id"],target/"accepted")
    restore_revision(args.project,target/"accepted","parent",target/"undo")
    restore_revision(args.project,target/"accepted","result",target/"redo")
    print(json.dumps({"demo":"SCRIPTED_NOT_REAL_OFS","candidate_id":proposal["candidate_id"],
        "preview":str(target/"proposal/candidate_preview.png"),"result_revision_id":accepted["revision_id"],
        "undo_redo":"saved_as_independent_revision_packets"},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
