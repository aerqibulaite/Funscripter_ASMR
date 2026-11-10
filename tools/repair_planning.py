"""Repair failed semantic targets without repeating accepted model calls."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from funscripter.planning_repair import repair_planning

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("project")
parser.add_argument("--planning", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
result = repair_planning(args.project, args.planning, args.output)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(2 if result["remaining_failure_count"] else 0)
