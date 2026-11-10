"""Run the repository's unittest suite directly, without a PYTHONPATH shell setup."""
import argparse
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pattern", default="test_*.py")
args = parser.parse_args()
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=args.pattern)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
