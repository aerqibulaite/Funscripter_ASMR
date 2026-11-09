import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml

from funscripter.audio import acoustic_signature
from funscripter.cli import main
from funscripter.media import sha256
from funscripter.project import analyze_project, atomic_json, create_project
from funscripter.tasks import task_summary


class AudioCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rate = 22050
        signal = np.zeros(rate * 2, dtype="<i2")
        signal[rate:] = (8000 * np.sin(2 * np.pi * 1000 * np.arange(rate) / rate)).astype("<i2")
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(signal.tobytes())
        self.project = self.create("first", "[00:00.100]静音\n[00:01.100]声音")
        self.cache = self.root / "analysis-first"
        self.original = analyze_project(self.project, self.cache)

    def create(self, name, text, **changes):
        subtitle = self.root / f"{name}.lrc"
        subtitle.write_text(text, encoding="utf-8")
        config = self.root / f"{name}.yaml"
        config.write_text(yaml.safe_dump({"audio": "track.wav", "subtitles": {"path": subtitle.name},
            "ffmpeg": "nonexistent-ffmpeg-for-test", **changes}), encoding="utf-8")
        output = self.root / f"project-{name}"
        create_project(config, output)
        return output

    def evidence(self, output):
        return json.loads((output / "audio_evidence.json").read_text(encoding="utf-8"))

    def reuse(self, project=None, name="reused", profile=None):
        return analyze_project(project or self.project, self.root / name, profile, self.cache)

    def update_cache_evidence(self, change):
        evidence = self.evidence(self.cache)
        change(evidence)
        atomic_json(self.cache / "audio_evidence.json", evidence)
        manifest = json.loads((self.cache / "analysis_manifest.json").read_text())
        manifest["artifacts"]["audio_evidence.json"] = sha256(self.cache / "audio_evidence.json")
        atomic_json(self.cache / "analysis_manifest.json", manifest)

    def test_hit_skips_extraction_keeps_source_and_produces_independent_bundle(self):
        before = {path.name: sha256(path) for path in self.cache.iterdir()}
        with patch("funscripter.audio.extract_audio", side_effect=AssertionError("FFT must not rerun")):
            result = self.reuse()
        self.assertEqual(result["cache"]["status"], "hit")
        self.assertNotEqual(result["analysis_id"], self.original["analysis_id"])
        self.assertEqual(result["cache"]["source_manifest_sha256"], before["analysis_manifest.json"])
        self.assertEqual(before, {path.name: sha256(path) for path in self.cache.iterdir()})
        for name, expected in result["artifacts"].items():
            self.assertEqual(sha256(self.root / "reused" / name), expected)
        self.assertEqual(sha256(self.cache / "audio_features.npz"), sha256(self.root / "reused/audio_features.npz"))
        self.assertEqual(self.evidence(self.cache)["cue_summaries"], self.evidence(self.root / "reused")["cue_summaries"])

    def test_subtitle_change_rebuilds_summaries_and_rebinds_project(self):
        project = self.create("new-subtitles", "[00:00.100]跨越整段")
        with patch("funscripter.audio.extract_audio", side_effect=AssertionError("no acoustic recomputation")):
            result = self.reuse(project)
        self.assertEqual(result["cache"]["status"], "hit")
        evidence = self.evidence(self.root / "reused")
        self.assertEqual(len(evidence["cue_summaries"]), 1)
        self.assertNotEqual(evidence["cue_summaries"], self.evidence(self.cache)["cue_summaries"])
        self.assertEqual(evidence["project_id"], result["project_id"])
        self.assertEqual(evidence["source_revision_id"], result["source_revision_id"])
        self.assertNotEqual(result["summary_subtitle_sha256"], self.original["summary_subtitle_sha256"])
        fresh = self.root / "fresh"
        analyze_project(project, fresh)
        self.assertEqual(evidence["cue_summaries"], self.evidence(fresh)["cue_summaries"])

    def test_generation_preset_changes_do_not_invalidate_acoustics(self):
        project = self.create("new-presets", "[00:00.100]静音\n[00:01.100]声音",
            generation={"initial_position": 40, "presets": {"x": {"low": 20, "high": 70, "period_ms": 800}}})
        with patch("funscripter.audio.extract_audio", side_effect=AssertionError("presets do not affect acoustics")):
            result = self.reuse(project)
        self.assertEqual(result["cache"]["status"], "hit")

    def test_reordered_band_mapping_still_reuses_compatible_features(self):
        profile = self.root / "reordered.yaml"
        profile.write_text("bands_hz:\n  high: [3000.0, 10000.0]\n  mid: [350.0, 3000.0]\n  low: [40.0, 350.0]\n", encoding="utf-8")
        with patch("funscripter.audio.extract_audio", side_effect=AssertionError("mapping order is not an acoustic change")):
            result = self.reuse(profile=profile)
        self.assertEqual(result["cache"]["status"], "hit")

    def test_audio_and_analysis_config_changes_miss_with_reason(self):
        profile = self.root / "analysis.yaml"
        profile.write_text("hop_ms: 20\n", encoding="utf-8")
        result = self.reuse(name="new-profile", profile=profile)
        self.assertEqual(result["cache"]["reasons"], ["analysis_config_changed"])
        self.assertEqual(self.evidence(self.root / "new-profile")["hop_samples"], 441)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 4)
        project = self.create("changed-audio", "[00:00.100]静音")
        result = self.reuse(project, name="new-audio")
        self.assertEqual(result["cache"]["reasons"], ["analysis_audio_changed"])
        self.assertEqual(result["cache"]["status"], "miss")

    def test_implementation_and_numpy_changes_miss(self):
        def changed_signature(config, input_hash):
            signature = acoustic_signature(config, input_hash)
            signature["implementation_sha256"] = "changed"
            return signature
        with patch("funscripter.audio.acoustic_signature", side_effect=changed_signature):
            result = self.reuse(name="new-code")
        self.assertEqual(result["cache"]["reasons"], ["extractor_implementation_changed"])
        with patch("funscripter.audio.np.__version__", "test-changed-version"):
            result = self.reuse(name="new-numpy")
        self.assertEqual(result["cache"]["reasons"], ["numpy_version_changed"])

    def test_corrupt_cache_refused_without_output_or_source_rewrite(self):
        path = self.cache / "audio_features.npz"
        path.write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "cache artifact changed"):
            self.reuse()
        self.assertEqual(path.read_bytes(), b"damaged")
        self.assertFalse((self.root / "reused").exists())
        self.assertFalse(list(self.root.glob(".funscripter-analysis-*")))

    def test_inconsistent_identity_and_feature_time_grid_refused(self):
        self.update_cache_evidence(lambda value: value.update(cache_key="wrong"))
        with self.assertRaisesRegex(ValueError, "identity is inconsistent"):
            self.reuse()

    def test_rehashed_feature_corruption_still_fails_structural_validation(self):
        path = self.cache / "audio_features.npz"
        with np.load(path, allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files}
        arrays["features"][1, 0] += 1
        np.savez_compressed(path, **arrays)
        manifest = json.loads((self.cache / "analysis_manifest.json").read_text())
        manifest["artifacts"]["audio_features.npz"] = sha256(path)
        atomic_json(self.cache / "analysis_manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "time grid"):
            self.reuse()
        self.assertFalse((self.root / "reused").exists())

    def test_reuse_output_cannot_be_inside_source_cache(self):
        with self.assertRaisesRegex(ValueError, "outside the reused analysis"):
            analyze_project(self.project, self.cache / "nested", reuse_analysis=self.cache)
        self.assertFalse((self.cache / "nested").exists())

    def test_cli_tracks_reuse_source_as_input_and_reports_hit(self):
        record = self.root / "reuse-task.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["analyze", str(self.project), "--output", str(self.root / "cli-reused"),
                "--reuse-analysis", str(self.cache), "--task-file", str(record)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["cache"]["status"], "hit")
        task = task_summary(record)
        self.assertIn(str(self.cache / "analysis_manifest.json"), task["input_hashes"])
        # Tasks recorded before the optional flag was introduced remain readable.
        old = json.loads(record.read_text())
        del old["arguments"]["reuse_analysis"]
        atomic_json(record, old)
        self.assertIsNone(task_summary(record)["arguments"]["reuse_analysis"])


if __name__ == "__main__":
    unittest.main()
