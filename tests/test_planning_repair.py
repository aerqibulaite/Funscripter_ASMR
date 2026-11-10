import json
import unittest
from funscripter.planning_repair import repair_planning
from funscripter.project import plan_project, reopen_project
from funscripter.fuse_project import load_semantics
import test_planner as fixtures


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.create()
        self.source = self.fixture.root / "failed-plan"
        self.output = self.fixture.root / "repaired-plan"
        seen = []
        def transport(config, messages, schema):
            payload = json.loads(messages[-1]["content"])
            if payload["task"] == "semantic":
                seen.append(payload)
                if len(seen) == 1:
                    return "invalid json", {}
            return fixtures.mock_transport(config, messages, schema)
        plan_project(self.project, self.source, self.fixture.profile_path, transport=transport)

    def test_only_failed_targets_are_requested_and_source_is_unchanged(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        original = json.loads((self.source / "semantic_events.json").read_text(encoding="utf-8"))
        failed = {row["cue_id"] for row in original["failures"]}
        seen = []
        def transport(config, messages, schema):
            payload = json.loads(messages[-1]["content"])
            self.assertEqual(payload["task"], "semantic")
            seen.extend(row["cue_id"] for row in payload["target_cues"])
            return fixtures.mock_transport(config, messages, schema)
        report = repair_planning(self.project, self.source, self.output, transport)
        self.assertEqual(set(seen), failed)
        self.assertEqual(report["remaining_failure_count"], 0)
        project, timeline, media, _ = reopen_project(self.project)
        result, _, manifest = load_semantics(self.output, project, timeline, media["duration_ms"])
        self.assertEqual([row for row in result["events"] if row["cue_id"] not in failed], original["events"])
        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    def test_tampered_source_is_rejected_before_creating_output(self):
        (self.source / "semantic_events.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source artifact changed"):
            repair_planning(self.project, self.source, self.output, fixtures.mock_transport)
        self.assertFalse(self.output.exists())
