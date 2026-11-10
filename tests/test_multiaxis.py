import contextlib
import copy
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

import yaml

from funscripter.cli import main
from funscripter.media import sha256
from funscripter.models import DeviceConfig
from funscripter.multiaxis import MultiAxisProfile, build_draft, check_budgets, export_multiaxis_draft
from funscripter.project import atomic_json, create_project
from funscripter.tasks import read_task


def independent(**changes):
    return {"enabled": True, "mode": "independent", "generation": {
        "initial_position": 50, "presets": {"small": {"low": 40, "high": 60, "period_ms": 1000}},
        "segments": [{"segment_id": "s", "start_ms": 0, "end_ms": 2000, "preset_id": "small"}]},
        "device": {"allowed_position_range": [0, 100], "max_speed_pos_per_sec": 100.0}, **changes}


def linked(source="L0", **changes):
    return {"enabled": True, "mode": "linked", "link": {"source_axis": source, "scale": -1.0, "offset": 100.0},
        "device": {"allowed_position_range": [0, 100], "max_speed_pos_per_sec": 100.0}, **changes}


class MultiAxisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 2 * 2)
        (self.root / "track.lrc").write_text("[00:00]合成测试", encoding="utf-8")
        self.project = self.make_project("project")
        self.profile_path = self.root / "axes.yaml"
        self.output = self.root / "draft"
        self.raw = {"axes": {"L0": independent(), "L1": linked(), "R2": {"enabled": False}}}

    def make_project(self, name, device=None):
        config = self.root / f"{name}.yaml"
        config.write_text(yaml.safe_dump({"audio": "track.wav", "subtitles": {"path": "track.lrc"},
            "ffmpeg": "nonexistent-ffmpeg-for-test", "device": device or {}}), encoding="utf-8")
        output = self.root / name
        create_project(config, output)
        return output

    def write_profile(self, raw=None):
        self.profile_path.write_text(yaml.safe_dump(self.raw if raw is None else raw), encoding="utf-8")

    def export(self, project=None):
        self.write_profile()
        return export_multiaxis_draft(project or self.project, self.profile_path, self.output)

    def test_independent_and_linked_curves_share_timeline_and_disabled_axes_stay_absent(self):
        before = {str(p.relative_to(self.project)): sha256(p) for p in self.project.rglob("*") if p.is_file()}
        manifest = self.export()
        self.assertEqual(manifest["axis_files"], {"L0": "track.funscript", "L1": "track.surge.funscript"})
        left = json.loads((self.output / "track.funscript").read_text())["actions"]
        right = json.loads((self.output / "track.surge.funscript").read_text())["actions"]
        self.assertEqual([p["at"] for p in left], [p["at"] for p in right])
        self.assertEqual([100 - p["pos"] for p in left], [p["pos"] for p in right])
        self.assertEqual((left[0]["at"], left[-1]["at"]), (0, 2000))
        self.assertFalse((self.output / "track.pitch.funscript").exists())
        self.assertEqual(before, {str(p.relative_to(self.project)): sha256(p) for p in self.project.rglob("*") if p.is_file()})
        for name, expected in manifest["artifact_hashes"].items():
            self.assertEqual(sha256(self.output / name), expected)
        self.assertEqual(manifest["purpose"], "offline_axis_contract_fixture")
        self.assertFalse(manifest["hardware_verified"])
        self.assertFalse(manifest["semantic_plan_applied"])

    def test_all_six_filenames_and_recursive_link_sources(self):
        self.raw["axes"] = {"L0": independent(), "L1": linked("R2"), "L2": linked("L1"),
            "R0": independent(), "R1": linked("L0"), "R2": linked("R0")}
        result = self.export()
        self.assertEqual(result["axis_files"], {"L0": "track.funscript", "L1": "track.surge.funscript",
            "L2": "track.sway.funscript", "R0": "track.twist.funscript", "R1": "track.roll.funscript",
            "R2": "track.pitch.funscript"})

    def test_auxiliary_axis_alone_does_not_implicitly_enable_l0(self):
        self.raw["axes"] = {"L1": independent()}
        result = self.export()
        self.assertEqual(result["enabled_axes"], ["L1"])
        self.assertFalse((self.output / "track.funscript").exists())

    def test_link_speed_failure_blocks_entire_group_but_preserves_diagnostics(self):
        self.raw["axes"]["L1"]["link"] = {"source_axis": "L0", "scale": 2.0, "offset": -50.0}
        self.raw["axes"]["L1"]["device"]["max_speed_pos_per_sec"] = 50.0
        result = self.export()
        self.assertTrue(result["export_blocked"])
        self.assertFalse(result["axis_files"])
        self.assertEqual(list(self.output.glob("*.funscript")), [])
        report = json.loads((self.output / "validation.json").read_text())
        self.assertIn("device_speed_limit", report["axis_reports"]["L1"]["constraint_errors"])
        self.assertTrue((self.output / "candidate_curves.json").exists())

    def test_inherited_project_l0_limits_cannot_be_widened(self):
        project = self.make_project("limited", {"allowed_position_range": [0, 100], "max_speed_pos_per_sec": 30.0})
        result = self.export(project)
        self.assertTrue(result["export_blocked"])
        report = json.loads((self.output / "validation.json").read_text())["axis_reports"]["L0"]
        self.assertEqual(report["effective_device"]["max_speed_pos_per_sec"], 30.0)
        self.assertIn("device_speed_limit", report["constraint_errors"])

    def test_conflicting_l0_ranges_and_invalid_link_positions_are_reported(self):
        self.raw["axes"]["L0"]["device"]["allowed_position_range"] = [60, 80]
        profile = MultiAxisProfile.model_validate_json(json.dumps(self.raw))
        curves, report = build_draft(profile, 2000, DeviceConfig(allowed_position_range=(45, 55)))
        self.assertTrue(report["export_blocked"])
        self.assertIn("ranges conflict", report["axis_reports"]["L0"]["error"])
        self.assertIsNone(curves["L1"])
        self.raw["axes"]["L0"] = independent()
        self.raw["axes"]["L1"]["link"] = {"source_axis": "L0", "scale": 3.0, "offset": 0.0}
        _, report = build_draft(MultiAxisProfile.model_validate_json(json.dumps(self.raw)), 2000, DeviceConfig())
        self.assertTrue(report["export_blocked"])
        self.assertIn("invalid_position", report["axis_reports"]["L1"]["constraint_errors"])

    def test_budget_checks_union_of_all_axis_turns_exactly(self):
        self.raw["position_budgets"] = [{"rule_id": "joint", "references": {"L0": 0, "L1": 0}, "max_total_deviation": 120.0}]
        profile = MultiAxisProfile.model_validate_json(json.dumps(self.raw))
        curves = {"L0": {"actions": [{"at": 0, "pos": 0}, {"at": 1000, "pos": 100}]},
            "L1": {"actions": [{"at": 0, "pos": 0}, {"at": 500, "pos": 100}, {"at": 1000, "pos": 0}]}}
        report = check_budgets(profile, curves, 1000)[0]
        self.assertFalse(report["passed"])
        self.assertEqual((report["maximum_exact"], report["at_ms"]), ("150", 500))
        # Union-grid interpolation has a rational result, not a rounded frame sample.
        curves["L1"]["actions"][1]["at"] = 333
        self.assertEqual(check_budgets(profile, curves, 1000)[0]["maximum_exact"], "1333/10")

    def test_budget_failure_blocks_scripts_even_when_each_axis_passes(self):
        self.raw["position_budgets"] = [{"rule_id": "tight", "references": {"L0": 50, "L1": 50}, "max_total_deviation": 1.0}]
        result = self.export()
        self.assertTrue(result["export_blocked"])
        report = json.loads((self.output / "validation.json").read_text())
        self.assertTrue(all(not value["constraint_errors"] for value in report["axis_reports"].values()))
        self.assertFalse(report["position_budgets"][0]["passed"])
        self.assertFalse(result["axis_files"])

    def test_invalid_contracts_are_rejected(self):
        variants = []
        raw = {"axes": {"L0": {"enabled": False}}}; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L0"] = {"enabled": False}; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L0"] = linked("L1"); variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L1"]["link"]["source_axis"] = "R2"; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["T0"] = independent(); variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L0"]["generation"] = None; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L1"]["generation"] = independent()["generation"]; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["axes"]["L1"]["link"]["scale"] = float("inf"); variants.append(raw)
        for basename in ("../escape", "CON", "nested/name"):
            raw = copy.deepcopy(self.raw); raw["basename"] = basename; variants.append(raw)
        raw = copy.deepcopy(self.raw); raw["position_budgets"] = [{"rule_id": "bad", "references": {"L0": 50, "R2": 50}, "max_total_deviation": 10.0}]; variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MultiAxisProfile.model_validate_json(json.dumps(raw))

    def test_invalid_segment_publishes_only_failure_diagnostics_and_cli_returns_2(self):
        self.raw["axes"]["L0"]["generation"]["segments"][0]["end_ms"] = 3000
        self.write_profile()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["multiaxis-draft", str(self.project), "--profile", str(self.profile_path),
                "--output", str(self.output), "--task-file", str(self.root / "task.json")])
        self.assertEqual(code, 2, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["execution_status"], "failed")
        self.assertFalse(list(self.output.glob("*.funscript")))
        report = json.loads((self.output / "validation.json").read_text())
        self.assertIn("inside audio", report["axis_reports"]["L0"]["error"])
        self.assertIn("source curve unavailable", report["axis_reports"]["L1"]["error"])

    def test_incomplete_limits_are_not_claimed_as_full_configuration(self):
        self.raw["axes"]["L1"]["device"] = {}
        result = self.export()
        self.assertFalse(result["export_blocked"])
        report = json.loads((self.output / "validation.json").read_text())
        self.assertFalse(report["per_axis_limits_fully_configured"])

    def test_no_overwrite_and_saved_manual_locks_are_respected(self):
        self.export()
        with self.assertRaisesRegex(ValueError, "new directory"):
            export_multiaxis_draft(self.project, self.profile_path, self.output)
        override = self.project / "manual_overrides.json"
        atomic_json(override, {"schema_version": "1.3", "overrides": [], "locked_ranges": [[0, 1000]]})
        project_path = self.project / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["artifact_hashes"]["overrides"] = sha256(override)
        atomic_json(project_path, project)
        with self.assertRaisesRegex(ValueError, "manual edits/locks"):
            export_multiaxis_draft(self.project, self.profile_path, self.root / "locked")

    def test_cli_and_batch_use_existing_task_protocol(self):
        self.write_profile()
        config = self.root / "batch.yaml"
        config.write_text(yaml.safe_dump({"jobs": [{"id": "axes", "command": "multiaxis-draft",
            "arguments": {"project": "project", "profile": "axes.yaml", "output": "run/axes"}}]}), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["batch", "--config", str(config), "--output", str(self.root / "run")])
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["counts"]["completed"], 1)
        task = read_task(self.root / "run/.batch/tasks/axes.json")
        self.assertEqual(task.command, "multiaxis-draft")
        self.assertIn(str(self.profile_path), task.input_hashes)


if __name__ == "__main__":
    unittest.main()
