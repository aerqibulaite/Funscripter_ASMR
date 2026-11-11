import json
import unittest
from unittest.mock import patch
from funscripter.planning_repair import repair_planning, recover_outline
from funscripter.planner import Planner, validate_outline
from funscripter.models import Outline
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
                if payload["target_cues"][0]["cue_id"] == "cue_000001":
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


class OutlineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.create()
        import yaml
        profile = yaml.safe_load(self.fixture.profile_path.read_text("utf-8"))
        profile["outline_max_phases"] = 2
        self.fixture.profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        self.source = self.fixture.root / "merge-failed"
        self.output = self.fixture.root / "merge-recovered"
        original_fits = Planner.fits
        def fits(planner, task, data, model):
            return False if task == "outline" else original_fits(planner, task, data, model)
        def transport(config, messages, schema):
            if json.loads(messages[-1]["content"])["task"] == "merge_outline":
                return "invalid", {}
            return fixtures.mock_transport(config, messages, schema)
        with patch.object(Planner, "fits", fits):
            run = plan_project(self.project, self.source, self.fixture.profile_path, transport=transport)
        self.assertEqual(run["global_input_strategy"], "hierarchical_merge_failed")
        self.assertIn("hierarchical_merge_failed", run["error"])

    def test_recovery_only_calls_merge_and_preserves_failed_source(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        def transport(config, messages, schema):
            self.assertEqual(json.loads(messages[-1]["content"])["task"], "merge_outline")
            return fixtures.mock_transport(config, messages, schema)
        report = recover_outline(self.project, self.source, self.output, transport)
        self.assertEqual(report["status"], "outline_recovered_semantics_pending")
        self.assertFalse(report["semantic_planning_complete"])
        self.assertEqual(report["covered_cue_count"], 8)
        self.assertEqual(report["reused_partial_count"], 4)
        _, timeline, _, _ = reopen_project(self.project)
        validate_outline(Outline.model_validate_json((self.output / "outline.json").read_text("utf-8")), timeline.cues)
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    def test_recovery_rejects_invalid_accepted_partial_before_output(self):
        path = next((self.source / "requests").glob("*/accepted.json"))
        data = json.loads(path.read_text("utf-8"))
        data["phases"][0]["evidence_cue_ids"] = ["unknown"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outline_evidence_outside_phase"):
            recover_outline(self.project, self.source, self.output, fixtures.mock_transport)
        self.assertFalse(self.output.exists())

    def test_recovered_profile_skips_all_outline_calls_and_rejects_tampering(self):
        recover_outline(self.project, self.source, self.output, fixtures.mock_transport)
        def semantic_only(config, messages, schema):
            self.assertEqual(json.loads(messages[-1]["content"])["task"], "semantic")
            return fixtures.mock_transport(config, messages, schema)
        run = plan_project(self.project, self.fixture.root / "continued",
                           self.output / "llm-recovered.yaml", transport=semantic_only)
        self.assertEqual(run["global_input_strategy"], "recovered_outline")
        self.assertEqual(run["failure_count"], 0)
        self.assertIsNotNone(run["outline_seed_sha256"])
        (self.output / "outline.json").write_text("{}", encoding="utf-8")
        target = self.fixture.root / "bad-seed-output"
        with self.assertRaisesRegex(ValueError, "source artifact changed"):
            plan_project(self.project, target, self.output / "llm-recovered.yaml", transport=semantic_only)
        self.assertFalse(target.exists())
