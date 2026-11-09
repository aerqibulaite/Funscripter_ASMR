import tempfile
import unittest
import wave
from pathlib import Path
import yaml
from funscripter.lrc import parse_lrc, candidate_document
from funscripter.models import Config, SubtitleConfig
from funscripter.motion import generate
from funscripter.project import create_project, reopen_project


class LrcTests(unittest.TestCase):
    def test_fraction_multitag_offset_raw_and_markers(self):
        timeline = parse_lrc("[offset:100]\n[00:00.5][00:01.50]原文 / 译文\n[00:02.050]\n[00:03.050]结束", 5000,
                             SubtitleConfig(path="x", offset_ms=-50))
        self.assertEqual([c.start_ms for c in timeline.cues], [550, 1550, 3100])
        self.assertEqual([c.original_start_ms for c in timeline.cues], [500, 1500, 3050])
        self.assertEqual(timeline.cues[0].text_raw, "原文 / 译文")
        self.assertEqual(timeline.cues[1].end_ms, 3100)
        self.assertEqual(timeline.cues[-1].end_ms, 5000)
        candidate = candidate_document(timeline)["cues"][1]
        self.assertEqual(candidate["utterance_end_candidates"][0]["time_ms"], 2100)
        self.assertIsNone(candidate["accepted_candidate"])

    def test_anomalies_preserved_and_ids_stable_with_offset(self):
        text = "[00:02]乙\n[00:01]甲\n[00:01]甲\n[-00:01]负数\n[00:09]越界"
        first = parse_lrc(text, 5000, SubtitleConfig(path="x"))
        second = parse_lrc(text, 5000, SubtitleConfig(path="x", offset_ms=100))
        self.assertEqual([c.cue_id for c in first.cues], [c.cue_id for c in second.cues])
        self.assertEqual({i["reason"] for i in first.issues},
                         {"out_of_order", "same_timestamp", "duplicate", "outside_audio"})
        self.assertEqual(len(first.cues), 5)
        self.assertIsNone(first.cues[-1].end_ms)

    def test_invalid_and_empty_inputs(self):
        for text in ["[00:60]bad", "[offset:abc]\n[00:01]x", "no timestamps"]:
            with self.assertRaises(ValueError):
                parse_lrc(text, 5000, SubtitleConfig(path="x"))


def config_dict():
    return {"audio": "track.wav", "subtitles": {"path": "track.lrc"},
        "ffmpeg": "nonexistent-ffmpeg-for-test",
        "generation": {"initial_position": 50, "presets": {
            "test": {"low": 40, "high": 60, "period_ms": 1000}}, "segments": [
            {"segment_id": "s1", "start_ms": 1000, "end_ms": 2500, "preset_id": "test"},
            {"segment_id": "s2", "start_ms": 3500, "end_ms": 4200, "preset_id": "test"}]}}


class MotionTests(unittest.TestCase):
    def test_gap_hold_final_time_and_unresolved_profile(self):
        script, report = generate(Config.model_validate(config_dict()), 5000)
        points = {p["at"]: p["pos"] for p in script["actions"]}
        self.assertEqual(points[0], points[1000])
        self.assertEqual(points[2500], points[3500])
        self.assertEqual(points[4200], points[5000])
        self.assertFalse(report["device_constraints_passed"])
        self.assertTrue(report["format_valid"])
        self.assertTrue(all(a["at"] < b["at"] for a, b in zip(script["actions"], script["actions"][1:])))

    def test_reject_speed_and_overlap_without_moving_anchors(self):
        data = config_dict()
        data["device"] = {"max_speed_pos_per_sec": 1.0}
        with self.assertRaisesRegex(ValueError, "device_speed_limit"):
            generate(Config.model_validate(data), 5000)
        data = config_dict()
        data["generation"]["segments"][1]["start_ms"] = 2400
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            generate(Config.model_validate(data), 5000)

    def test_resolved_profile_and_asymmetric_cycle(self):
        data = config_dict()
        data["generation"]["presets"]["test"]["up_time_ratio"] = 0.25
        data["device"] = {"allowed_position_range": (30, 70), "max_speed_pos_per_sec": 100.0}
        script, report = generate(Config.model_validate(data), 5000)
        self.assertTrue(report["device_constraints_passed"])
        self.assertIn({"at": 1250, "pos": 60}, script["actions"])
        self.assertIn({"at": 2000, "pos": 40}, script["actions"])
        data["device"]["allowed_position_range"] = (45, 55)
        with self.assertRaisesRegex(ValueError, "device_position_range"):
            generate(Config.model_validate(data), 5000)

    def test_strict_config_rejects_unknown_and_wrong_types(self):
        for change in [{"typo": True}, {"subtitles": {"path": "x", "offset_ms": "10"}}]:
            with self.assertRaises(ValueError):
                Config.model_validate(config_dict() | change)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((2, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * (22050 * 5 * 4))
        (self.root / "track.lrc").write_text("[00:00.5]准备\n[00:03]结束", encoding="utf-8-sig")
        (self.root / "config.yaml").write_text(yaml.safe_dump(config_dict()), encoding="utf-8")

    def test_saved_project_reopens_without_originals_and_detects_external_edits(self):
        output = self.root / "project"
        project = create_project(self.root / "config.yaml", output)
        (self.root / "track.wav").unlink()
        (self.root / "track.lrc").unlink()
        loaded, timeline, media, report = reopen_project(output)
        self.assertEqual(loaded.project_id, project.project_id)
        self.assertEqual(media["sample_count"], 22050 * 5)
        self.assertEqual(media["duration_ms"], 5000)
        self.assertEqual(len(timeline.cues), 2)
        self.assertTrue(report["format_valid"])
        script_path = output / "exports/track.funscript"
        script_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            reopen_project(output)

    def test_existing_project_never_overwritten(self):
        output = self.root / "project"
        create_project(self.root / "config.yaml", output)
        before = (output / "project.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_project(self.root / "config.yaml", output)
        self.assertEqual(before, (output / "project.json").read_bytes())

    def test_failed_build_leaves_no_partial_project(self):
        data = config_dict()
        data["generation"]["segments"][0]["end_ms"] = 9999
        (self.root / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            create_project(self.root / "config.yaml", self.root / "project")
        self.assertFalse((self.root / "project").exists())
        self.assertFalse(list(self.root.glob(".funscripter-*")))


if __name__ == "__main__":
    unittest.main()
