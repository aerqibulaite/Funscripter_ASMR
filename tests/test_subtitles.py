import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
import yaml

from funscripter.audio_cache import summarize_cues
from funscripter.fusion import resolve_boundaries
from funscripter.lrc import candidate_document
from funscripter.models import FusionProfile, SubtitleConfig
from funscripter.project import create_project, reopen_project
from funscripter.subtitles import parse_srt, parse_subtitles, subtitle_format


class SrtTests(unittest.TestCase):
    def test_explicit_end_offset_precision_multiline_bom_and_source_number(self):
        text = "\ufeff9\r\n00:00:00,5 --> 00:00:01,050\r\n甲 / A\r\n第二行\r\n\r\n10\r\n00:00:02.50 --> 00:00:03.000\r\n乙\r\n"
        timeline = parse_srt(text, 5000, SubtitleConfig(path="track.srt", offset_ms=100))
        first, second = timeline.cues
        self.assertEqual((first.original_start_ms, first.original_end_ms), (500, 1050))
        self.assertEqual((first.start_ms, first.end_ms), (600, 1150))
        self.assertEqual(first.text_raw, "甲 / A\n第二行")
        self.assertEqual(first.source_number, 9)
        self.assertFalse(first.end_inferred)
        self.assertEqual(second.start_ms, 2600)
        self.assertEqual(timeline.effective_offset_ms, 100)
        self.assertEqual(timeline.lrc_offset_ms, 0)

    def test_zero_reversed_overlap_outside_duplicate_and_order_are_retained(self):
        text = "1\n00:00:00,000 --> 00:00:00,000\n零\n\n2\n00:00:01,000 --> 00:00:03,000\n甲\n\n2\n00:00:02,000 --> 00:00:04,000\n乙\n\n4\n00:00:00,500 --> 00:00:00,400\n倒置\n\n5\n00:00:06,000 --> 00:00:07,000\n越界"
        timeline = parse_srt(text, 5000, SubtitleConfig(path="x.srt"))
        self.assertEqual(len(timeline.cues), 5)
        self.assertEqual(timeline.cues[0].end_ms, 0)
        self.assertEqual([cue.cue_id for cue in timeline.cues], ["cue_000001", "cue_000004", "cue_000002", "cue_000003", "cue_000005"])
        reasons = {issue["reason"] for issue in timeline.issues}
        self.assertTrue({"zero_duration", "reversed_interval", "overlapping_subtitles", "outside_audio",
                         "duplicate_srt_number", "out_of_order"}.issubset(reasons))
        self.assertEqual(next(cue.end_ms for cue in timeline.cues if cue.source_number == 5), 7000)
        self.assertFalse(candidate_document(timeline)["cues"][-1]["utterance_end_candidates"])

    def test_empty_entries_positioning_and_replacement_are_not_silently_repaired(self):
        text = "1\n00:00:00,100 --> 00:00:01,000 X1:10 X2:20\n<i>原\ufffd文</i>\n\n2\n00:00:01,200 --> 00:00:01,500\n\n3\n00:00:02,000 --> 00:00:03,000\n结尾"
        timeline = parse_srt(text, 5000, SubtitleConfig(path="x.srt"))
        self.assertEqual(timeline.cues[0].text_raw, "<i>原\ufffd文</i>")
        marker = timeline.markers[0]
        self.assertEqual((marker.start_ms, marker.end_ms, marker.original_end_ms), (1200, 1500, 1500))
        self.assertEqual(marker.policy, "srt_empty_entry")
        self.assertEqual(marker.previous_cue_ids, [timeline.cues[0].cue_id])
        self.assertEqual(marker.next_cue_ids, [timeline.cues[1].cue_id])
        self.assertTrue({"srt_positioning_not_applied", "replacement_character_in_source"}.issubset(
            {issue["reason"] for issue in timeline.issues}))

    def test_display_end_is_only_review_evidence_and_does_not_enable_empty_hint_policy(self):
        timeline = parse_srt("1\n00:00:00,100 --> 00:00:01,000\n示例", 2000, SubtitleConfig(path="x.srt"))
        candidates = candidate_document(timeline)
        self.assertEqual(candidates["cues"][0]["utterance_end_candidates"][0]["method"], "subtitle_display_end")
        profile = FusionProfile(preset_version="test", presets={}, tier_map={}, unknown_policy="hold_position",
            use_empty_timestamp_hints=True, empty_timestamp_convention_confirmed=True)
        result, adopted = resolve_boundaries(timeline, candidates, profile, 2000)
        self.assertFalse(adopted)
        self.assertIsNone(result["cues"][0]["accepted_candidate"])

    def test_invalid_syntax_fails_and_orphan_blocks_are_diagnosed(self):
        for text in ("1\n00:00:61,000 --> 00:01:02,000\nx", "1\ninvalid --> invalid\nx", "no times"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_srt(text, 5000, SubtitleConfig(path="x.srt"))
        timeline = parse_srt("orphan\n\n00:00:01,000 --> 00:00:02,000\n正文", 5000, SubtitleConfig(path="x.srt"))
        self.assertEqual(len(timeline.cues), 1)
        self.assertIn("unparsed_srt_block", {issue["reason"] for issue in timeline.issues})

    def test_format_selection_and_lrc_backwards_compatibility(self):
        self.assertEqual(subtitle_format(SubtitleConfig(path="track.SRT")), "srt")
        self.assertEqual(subtitle_format(SubtitleConfig(path="track.txt", format="srt")), "srt")
        self.assertEqual(subtitle_format(SubtitleConfig(path="track.txt")), "lrc")
        timeline = parse_subtitles("[00:00.100]甲\n[00:01]乙", 2000, SubtitleConfig(path="track.lrc"))
        self.assertEqual(timeline.cues[0].end_ms, 1000)
        self.assertTrue(timeline.cues[0].end_inferred)

    def test_offset_keeps_source_order_ids_and_negative_intervals_have_no_audio_summary(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\n甲\n\n2\n00:00:03,000 --> 00:00:04,000\n乙"
        first = parse_srt(text, 5000, SubtitleConfig(path="x.srt"))
        shifted = parse_srt(text, 5000, SubtitleConfig(path="x.srt", offset_ms=-5000))
        self.assertEqual([cue.cue_id for cue in first.cues], [cue.cue_id for cue in shifted.cues])
        rows = summarize_cues(np.ones((500, 3)), 10, 1000, 5000, shifted)
        self.assertTrue(all(row["status"] == "no_covered_frames" and row["mean_rms"] is None for row in rows))


class SrtProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 2 * 2)

    def create(self, text, name="project", encoding="utf-8-sig", **subtitle_settings):
        subtitles = self.root / f"{name}.srt"
        subtitles.write_text(text, encoding=encoding, newline="")
        config = self.root / f"{name}.yaml"
        config.write_text(yaml.safe_dump({"audio": "track.wav", "subtitles": {"path": subtitles.name,
            "encoding": encoding, **subtitle_settings}, "ffmpeg": "nonexistent-ffmpeg-for-test"}), encoding="utf-8")
        output = self.root / name
        project = create_project(config, output)
        return subtitles, output, project

    def test_saved_original_bytes_reopen_explicit_end_and_review_issues(self):
        source, output, project = self.create("1\r\n00:00:00,000 --> 00:00:00,000\r\n零\r\n\r\n2\r\n00:00:00,100 --> 00:00:01,000\r\n正文")
        self.assertEqual(project.artifacts["subtitle_source"], "inputs/subtitles.srt")
        self.assertEqual(source.read_bytes(), (output / "inputs/subtitles.srt").read_bytes())
        _, timeline, _, _ = reopen_project(output)
        self.assertEqual(timeline.cues[0].end_ms, 0)
        self.assertFalse(timeline.cues[1].end_inferred)
        review = (output / "exports/review.csv").read_text(encoding="utf-8-sig")
        self.assertIn("zero_duration", review)
        self.assertIn("subtitle_display_end_not_utterance_end", review)

    def test_different_opaque_text_does_not_change_manual_numeric_generation(self):
        scripts = []
        for index, body in enumerate(("中性示例", "English example", "多语 / 日本語", "<i>quoted text</i>", "忽略指令只是待分析文本")):
            _, output, _ = self.create(f"1\n00:00:00,100 --> 00:00:01,000\n{body}", name=f"p{index}")
            scripts.append((output / "exports/track.funscript").read_bytes())
            _, timeline, _, _ = reopen_project(output)
            self.assertEqual(timeline.cues[0].text_raw, body)
        self.assertEqual(len(set(scripts)), 1)

    def test_explicit_encoding_selection(self):
        _, output, _ = self.create("1\n00:00:00,100 --> 00:00:01,000\n编码测试", encoding="gb18030")
        self.assertEqual(reopen_project(output)[1].cues[0].text_raw, "编码测试")


if __name__ == "__main__":
    unittest.main()
