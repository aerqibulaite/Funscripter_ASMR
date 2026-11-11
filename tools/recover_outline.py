"""Recover a failed global merge, reusing accepted partial outlines."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from funscripter.planning_repair import recover_outline

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("project")
parser.add_argument("--planning", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
report = recover_outline(args.project, args.planning, args.output)
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(1 if report["status"] == "failed" else 0)
