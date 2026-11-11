import json
import unittest
import shutil
import subprocess
from pathlib import Path

from funscripter.fuse_project import fuse_project
from funscripter.interactive_preview import create_interactive_preview, curve_summary
import test_fusion as fixtures
from test_fusion import make_profile


class InteractivePreviewTests(unittest.TestCase):
    setUp = fixtures.FusionTests.setUp
    create = fixtures.FusionTests.create
    publish_inputs = fixtures.FusionTests.publish_inputs

    def test_actual_curve_statistics_merge_static_intervals(self):
        stats, holds = curve_summary([{"at": 0, "pos": 50}, {"at": 1000, "pos": 50},
            {"at": 2000, "pos": 50}, {"at": 3000, "pos": 60}, {"at": 4000, "pos": 50},
            {"at": 15000, "pos": 50}], 15000)
        self.assertEqual(stats["moving_ms"], 2000)
        self.assertEqual(stats["holding_ms"], 13000)
        self.assertEqual(stats["longest_hold_ms"], 11000)
        self.assertEqual(stats["direction_changes"], 1)
        self.assertEqual(stats["max_speed"], 10)
        self.assertEqual(holds[0], {"start_ms": 0, "end_ms": 2000})

    def test_verified_export_contains_audio_inline_data_and_safe_source_text(self):
        # Imported text must remain text even when it contains HTML/script-looking strings.
        (self.root / "track.lrc").write_text('[00:00.000]</script><script>alert(1)</script>\n[00:02.000]继续', encoding="utf-8")
        self.create()
        planning, profile = self.publish_inputs()
        draft = self.root / "draft"
        fuse_project(self.project_dir, planning, draft, profile)
        output = self.root / "interactive"
        result = create_interactive_preview(self.project_dir, draft, output, planning)
        html = (output / "index.html").read_text("utf-8")
        raw = html.split('id="previewData">', 1)[1].split('</script>', 1)[0]
        data = json.loads(raw)
        self.assertIn('</script><script>', data["cues"][0]["text_raw"])
        self.assertNotIn('</script><script>', raw)
        self.assertTrue(data["has_semantics"])
        self.assertTrue((output / "audio.wav").is_file())
        self.assertEqual(len(result["artifact_hashes"]), 4)
        self.assertEqual(data["duration_ms"], 10000)
        self.assertEqual(len(data["waveform"]["low"]), 4096)
        if shutil.which("node"):
            result = subprocess.run(["node", str(Path(__file__).with_name("preview_viewer_check.cjs")), str(output)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_interactive_preview(self.project_dir, draft, output)
        (draft / "generated_base.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            create_interactive_preview(self.project_dir, draft, self.root / "bad")

    def test_blocked_curve_stays_visible_and_wrong_planning_is_refused(self):
        self.config["device"] = {"allowed_position_range": [0, 100], "max_speed_pos_per_sec": 1.0}
        self.create()
        planning, profile = self.publish_inputs(make_profile())
        draft = self.root / "blocked-draft"
        fuse_project(self.project_dir, planning, draft, profile)
        result = create_interactive_preview(self.project_dir, draft, self.root / "blocked-preview")
        self.assertTrue(result["export_blocked"])
        self.assertFalse((draft / "track.funscript").exists())
        with self.assertRaisesRegex(ValueError, "independent"):
            create_interactive_preview(self.project_dir, draft, self.project_dir / "preview")
        (planning / "planning_run.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "planning does not match"):
            create_interactive_preview(self.project_dir, draft, self.root / "wrong", planning)


if __name__ == "__main__":
    unittest.main()
