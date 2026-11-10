import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
import yaml

from test_planner import mock_transport

from funscripter.cli import execute_stage
from funscripter.gui_backend import DesktopController
from funscripter.lrc import candidate_document
from funscripter.media import decode_audio, sha256
from funscripter.models import SubtitleConfig
from funscripter.project import plan_project, reopen_project
from funscripter.subtitles import parse_subtitles, parse_vtt, subtitle_format


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg") or str(ROOT / "workspace/tools/ffmpeg-binaries/ffmpeg.exe")
FFPROBE = shutil.which("ffprobe") or str(ROOT / "workspace/tools/ffmpeg-binaries/ffprobe.exe")


class VttTests(unittest.TestCase):
    def parse(self, text, offset=0):
        return parse_vtt(text, 10000, SubtitleConfig(path="test.vtt", offset_ms=offset))

    def test_short_long_timestamps_identifiers_multiline_and_bom(self):
        text = "\ufeffWEBVTT example\r\nLanguage: zh\r\n\r\nchapter-A\r\n00:00.100 --> 00:01.250\r\n第一行\r\nsecond line\r\n\r\n23\r\n00:00:02.000 --> 00:00:03.000\r\n结尾"
        timeline = self.parse(text, 50)
        a, b = timeline.cues
        self.assertEqual((a.original_start_ms, a.original_end_ms, a.start_ms, a.end_ms), (100, 1250, 150, 1300))
        self.assertEqual(a.text_raw, "第一行\nsecond line")
        self.assertEqual((a.source_line, a.source_identifier, b.source_identifier), (4, "chapter-A", "23"))
        self.assertEqual(timeline.metadata["vtt_header_metadata"], "Language: zh")
        self.assertFalse(a.end_inferred)
        self.assertEqual(subtitle_format(SubtitleConfig(path="test.VTT")), "vtt")
        self.assertEqual(parse_subtitles(text, 10000, SubtitleConfig(path="test.txt", format="vtt")), self.parse(text))

    def test_non_cue_blocks_layout_and_markup_preserved(self):
        text = "WEBVTT\n\nNOTE comment\n00:00.000 --> ignored\n\nSTYLE\n::cue { color: lime; }\n\nREGION\nid:main\n\nvoice\n00:01.000 --> 00:02.000 region:main align:start\n<v Speaker><i>A &amp; B</i> <00:01.500>word"
        timeline = self.parse(text)
        self.assertEqual(len(timeline.cues), 1)
        self.assertIn("<v Speaker>", timeline.cues[0].text_raw)
        self.assertEqual(timeline.cues[0].source_settings, "region:main align:start")
        issues = [issue["reason"] for issue in timeline.issues]
        self.assertEqual(issues.count("vtt_non_cue_block_not_rendered"), 3)
        self.assertIn("vtt_positioning_not_applied", issues)
        self.assertIn("vtt_payload_markup_preserved", issues)
        self.assertEqual(timeline.cues[0].speaker_label, None)

    def test_anomalies_stable_ids_and_review_only_end(self):
        text = "WEBVTT\n\nx\n00:01.000 --> 00:03.000\nA\n\nx\n00:02.000 --> 00:04.000\nB\n\n00:00.500 --> 00:00.500\nZero\n\n00:05.000 --> 00:04.000\nReverse\n\n00:11.000 --> 00:12.000\nOutside"
        first, shifted = self.parse(text), self.parse(text, -1000)
        self.assertEqual([cue.cue_id for cue in first.cues], [cue.cue_id for cue in shifted.cues])
        self.assertTrue({"duplicate_vtt_identifier", "out_of_order", "overlapping_subtitles", "zero_duration", "reversed_interval", "outside_audio"}.issubset({i["reason"] for i in first.issues}))
        rows = candidate_document(first)["cues"]
        self.assertEqual(sum(len(row["utterance_end_candidates"]) for row in rows), 2)
        self.assertTrue(all(row["accepted_candidate"] is None for row in rows))

    def test_empty_entry_is_not_an_end_hint(self):
        timeline = self.parse("WEBVTT\n\nempty\n00:01.000 --> 00:02.000\n\n00:03.000 --> 00:04.000\ntext")
        self.assertEqual(timeline.markers[0].policy, "vtt_empty_entry")
        self.assertEqual(timeline.markers[0].source_identifier, "empty")
        self.assertEqual(timeline.markers[0].end_ms, 2000)

    def test_invalid_times_and_unmapped_hls_clocks_fail_explicitly(self):
        for text in ("00:01.000 --> 00:02.000\nA", "WEBVTT\n00:01.000 --> 00:02.000\nA",
                     "WEBVTT\n\n00:60.000 --> 01:02.000\nA", "WEBVTT\n\n00:01,000 --> 00:02.000\nA",
                     "WEBVTT\n\n00:01.00 --> 00:02.000\nA", "WEBVTT\n\nNOTE only comments",
                     "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n00:01.000 --> 00:02.000\nA"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), "FFmpeg and FFprobe required")
class VideoImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "合成多音轨.mkv"
        cls.run_tool([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=3",
            "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(cls.video)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def run_tool(args):
        result = subprocess.run(args, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))

    def read(self, path):
        with wave.open(str(path), "rb") as stream:
            rate = stream.getframerate()
            return rate, np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")

    def test_audio_selection_leading_silence_and_video_tail(self):
        before = sha256(self.video)
        for index, delay in ((0, 0.5), (1, 1.0)):
            output = self.root / f"selected-{index}.wav"
            media = decode_audio(self.video, output, FFMPEG, FFPROBE, index)
            rate, samples = self.read(output)
            self.assertEqual((media["source_kind"], media["selected_audio_stream"]), ("video", index))
            self.assertEqual(len(samples), rate * 3)
            self.assertEqual(np.max(np.abs(samples[:int((delay - .05) * rate)])), 0)
            self.assertGreater(np.std(samples[int((delay + .1) * rate):int((delay + .2) * rate)]), 100)
            self.assertEqual(np.max(np.abs(samples[int((delay + 1.1) * rate):])), 0)
            self.assertFalse(media["compressed_source_sync_verified"])
        self.assertEqual(sha256(self.video), before)

    def test_nonzero_container_origin_does_not_add_false_duration(self):
        shifted = self.root / "shifted.mkv"
        self.run_tool([FFMPEG, "-v", "error", "-y", "-i", str(self.video), "-map", "0", "-c", "copy", "-output_ts_offset", "5", str(shifted)])
        output = self.root / "shifted.wav"
        media = decode_audio(shifted, output, FFMPEG, FFPROBE)
        rate, samples = self.read(output)
        self.assertEqual(media["container_origin_seconds"], 5.0)
        self.assertEqual(len(samples), rate * 3)
        self.assertEqual(np.max(np.abs(samples[:int(.45 * rate)])), 0)
        self.assertGreater(np.std(samples[int(.6 * rate):int(.7 * rate)]), 100)

    def test_mp4_compressed_audio_preserves_delayed_start(self):
        video = self.root / "compressed.mp4"
        self.run_tool([FFMPEG, "-v", "error", "-y", "-i", str(self.video), "-map", "0:v:0", "-map", "0:a:0",
                       "-c:v", "mpeg4", "-c:a", "aac", str(video)])
        output = self.root / "compressed.wav"
        media = decode_audio(video, output, FFMPEG, FFPROBE)
        rate, samples = self.read(output)
        self.assertLessEqual(abs(len(samples) - 3 * rate), 1)
        self.assertEqual(np.max(np.abs(samples[:int(.4 * rate)])), 0)
        self.assertGreater(np.std(samples[int(.6 * rate):int(.7 * rate)]), 100)
        self.assertTrue(media["synchronization_review_required"])

    def test_missing_audio_invalid_stream_and_probe_failure(self):
        with self.assertRaisesRegex(ValueError, "audio stream 2"):
            decode_audio(self.video, self.root / "bad.wav", FFMPEG, FFPROBE, 2)
        silent = self.root / "no-audio.mp4"
        self.run_tool([FFMPEG, "-v", "error", "-y", "-i", str(self.video), "-an", "-c:v", "mpeg4", str(silent)])
        with self.assertRaisesRegex(ValueError, "available audio streams: 0"):
            decode_audio(silent, self.root / "empty.wav", FFMPEG, FFPROBE)
        with self.assertRaisesRegex(ValueError, "requires FFprobe"):
            decode_audio(silent, self.root / "no-probe.wav", FFMPEG, "missing-probe-test")

    def test_desktop_video_vtt_project_and_original_bytes(self):
        subtitle = self.root / "字幕.vtt"
        subtitle.write_bytes("\ufeffWEBVTT\r\n\r\ncue-A\r\n00:01.100 --> 00:01.500\r\n中性示例".encode("utf-8"))
        controller = DesktopController(self.root / "desktop")
        result = controller.create(str(self.video), str(subtitle), FFMPEG, "utf-8-sig", "0", FFPROBE, "1")
        project, timeline, media, report = reopen_project(result["output"])
        self.assertEqual(media["duration_ms"], 3000)
        self.assertEqual(timeline.cues[0].start_ms, 1100)
        self.assertEqual(project.config.audio_stream, 1)
        self.assertTrue(report["format_valid"])
        self.assertEqual(subtitle.read_bytes(), (Path(result["output"]) / "inputs/subtitles.vtt").read_bytes())
        self.assertIn("video_container_timeline_requires_sync_review", (Path(result["output"]) / "exports/review.csv").read_text(encoding="utf-8-sig"))
        # Run the actual downstream pipeline; only model inference is simulated.
        project_dir = result["output"]
        analysis = controller.stage("analyze", project=project_dir)
        boundaries = controller.stage("utterance-candidates", project=project_dir, analysis=analysis["output"])
        model_profile = self.root / "model.yaml"
        model_profile.write_text(yaml.safe_dump({"backend": "ollama", "base_url": "http://127.0.0.1:1", "model_id": "mock-only"}), encoding="utf-8")
        def execute(command, arguments):
            if command == "plan":
                return plan_project(arguments["project"], arguments["output"], arguments["profile"], transport=mock_transport)
            return execute_stage(command, arguments)
        planned = DesktopController(controller.workspace, executor=execute).stage("plan", project=project_dir, profile=str(model_profile))
        fused = controller.stage("fuse", project=project_dir, planning=planned["output"], analysis=analysis["output"],
            boundaries=boundaries["output"], profile=str(ROOT / "config/fusion-example.yaml"))
        self.assertEqual(fused["execution_status"], "completed")
        self.assertTrue((Path(fused["output"]) / "track.funscript").is_file())


if __name__ == "__main__":
    unittest.main()
