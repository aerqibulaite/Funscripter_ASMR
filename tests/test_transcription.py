"""Offline integration tests for source separation, timing and bundle integrity."""
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as Row
from unittest.mock import patch

import yaml

from funscripter.gui_backend import DesktopController
from funscripter.models import LLMConfig
from funscripter.planner import Planner
from funscripter.project import plan_project, reopen_project
from funscripter.transcription import load_profile, probe_whisper, transcribe_audio, verify_transcription
from funscripter import transcription
from test_planner import mock_transport


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audio = self.root / "track.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 32000)
        model = self.root / "model"
        model.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (model / name).write_text("fixture", encoding="utf-8")
        self.profile = self.root / "asr.yaml"
        self.profile.write_text(yaml.safe_dump({"model_path": "model"}), encoding="utf-8")
        self.segment = Row(start=0.1, end=1.5, text=" 続けて ", avg_logprob=-0.2,
            no_speech_prob=0.01, words=[Row(start=0.1, end=1.5, word="続けて", probability=0.8)])
        self.calls = []

    def factory(self, model, **options):
        self.calls.append((model, options))
        def transcribe(audio, **kwargs):
            self.calls.append((audio, kwargs))
            return iter([self.segment]), Row(language="ja")
        return Row(transcribe=transcribe)

    @unittest.skipUnless(os.name == "nt", "Windows DLL search paths")
    def test_nvidia_wheel_dll_directories_persist_without_duplicate_registration(self):
        library = self.root / "bin" / "cublas64_12.dll"
        library.parent.mkdir()
        library.touch()
        installed = Row(files=[Path("bin/cublas64_12.dll")],
                        locate_file=lambda name: self.root / name)
        with patch("funscripter.transcription.distribution", return_value=installed), \
                patch("funscripter.transcription.os.add_dll_directory") as register, \
                patch.dict(transcription._CUDA_DLL_HANDLES, {}, clear=True), \
                patch.dict(os.environ, {"PATH": "existing-runtime-path"}):
            transcription.configure_windows_cuda_libraries()
            transcription.configure_windows_cuda_libraries()
            register.assert_called_once_with(str(library.parent.resolve()))
            self.assertIs(transcription._CUDA_DLL_HANDLES[library.parent.resolve()], register.return_value)
            self.assertEqual(os.environ["PATH"], str(library.parent.resolve()) + os.pathsep + "existing-runtime-path")

    def transcribe(self, output=None):
        return transcribe_audio(self.audio, output or self.root / "asr", self.profile,
                                ffmpeg="missing-ffmpeg-test", model_factory=self.factory)

    def test_bundle_retains_raw_words_and_is_bound_to_audio_and_model(self):
        result = self.transcribe()
        output = Path(result["output"])
        data = json.loads((output / "transcript.json").read_text("utf-8"))
        self.assertEqual(data["segments"][0]["text"], " 続けて ")
        self.assertEqual(data["segments"][0]["words"][0]["start_ms"], 100)
        self.assertIn("00:00:00,100 --> 00:00:01,500", (output / "subtitles.srt").read_text("utf-8"))
        self.assertTrue(self.calls[0][1]["local_files_only"])
        self.assertEqual(self.calls[1][1]["task"], "transcribe")
        self.assertEqual(len(result["model_hashes"]), 3)
        verify_transcription(output / "transcription_manifest.json", self.audio, output / "subtitles.srt", 0)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.transcribe()
        (output / "transcript.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            verify_transcription(output / "transcription_manifest.json", self.audio, output / "subtitles.srt", 0)

    def test_load_probe_and_existing_transcript_project_do_not_run_recognition_again(self):
        checked = probe_whisper(load_profile(self.profile), model_factory=self.factory)
        self.assertEqual(checked["status"], "load_test_passed")
        self.assertEqual(len(self.calls), 1)  # Factory called; transcription method was not.
        transcript = self.transcribe()
        controller = DesktopController(self.root / "desktop")
        with patch("funscripter.transcription.transcribe_audio", side_effect=AssertionError("must reuse saved transcript")):
            created = controller.create(str(self.audio), "", "missing-ffmpeg-test", "utf-8", "0",
                source_mode="本地 Whisper 转写", transcription_directory=transcript["output"])
        self.assertEqual(controller.inspect(created["output"])["text_input_kind"], "whisper")
        with self.assertRaisesRegex(ValueError, "source/subtitle/audio stream mismatch"):
            verify_transcription(Path(transcript["output"]) / "transcription_manifest.json",
                self.audio, Path(transcript["output"]) / "subtitles.srt", 1)

    def test_invalid_timestamps_and_empty_recognition_leave_no_published_bundle(self):
        for name, start, end, text in [("nan", float("nan"), 1, "x"),
                ("outside", 0, 3, "x"), ("reversed", 1, 0, "x"), ("empty", 0.1, 1.5, " ")]:
            with self.subTest(name=name):
                self.segment.start, self.segment.end, self.segment.text = start, end, text
                target = self.root / name
                with self.assertRaises(ValueError):
                    self.transcribe(target)
                self.assertFalse(target.exists())

    def test_gui_audio_only_project_selects_whisper_prompt_and_preserves_transcript(self):
        controller = DesktopController(self.root / "desktop")
        def transcribe(*args, **kwargs):
            return transcribe_audio(*args, **kwargs, model_factory=self.factory)
        with patch("funscripter.transcription.transcribe_audio", side_effect=transcribe):
            created = controller.create(str(self.audio), "", "missing-ffmpeg-test", "utf-8", "0",
                source_mode="本地 Whisper 转写", whisper_profile=str(self.profile))
        project, timeline, _, _ = reopen_project(created["output"])
        self.assertEqual(project.config.subtitles.source_kind, "whisper")
        self.assertEqual(timeline.metadata["language"], "ja")
        self.assertIn("transcript.json", project.artifacts)
        model_profile = self.root / "llm.yaml"
        model_profile.write_text(yaml.safe_dump(LLMConfig(backend="ollama", base_url="http://localhost:1",
            model_id="fixture").model_dump(mode="json")), encoding="utf-8")
        seen = []
        def transport(config, messages, schema):
            seen.append(messages)
            return mock_transport(config, messages, schema)
        result = plan_project(created["output"], self.root / "planning", model_profile, transport=transport)
        self.assertEqual(result["failure_count"], 0)
        self.assertIn("自动语音识别", seen[0][0]["content"])
        request = next(json.loads(messages[-1]["content"]) for messages in seen
                       if json.loads(messages[-1]["content"])["task"] == "semantic")
        self.assertEqual(request["target_cues"][0]["end_time_source"], "asr_segment_estimate")
        subtitle = self.root / "manual.srt"
        subtitle.write_text("1\n00:00:00,100 --> 00:00:01,500\n继续\n", encoding="utf-8")
        imported = controller.create(str(self.audio), str(subtitle), "missing-ffmpeg-test", "utf-8", "0")
        self.assertEqual(controller.inspect(imported["output"])["text_input_kind"], "subtitles")
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture")
        self.assertNotEqual(Planner(config, "unused").implementation_hash,
                            Planner(config, "unused", input_kind="whisper").implementation_hash)


if __name__ == "__main__":
    unittest.main()
