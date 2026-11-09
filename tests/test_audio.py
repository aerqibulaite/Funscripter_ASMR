import json
import tempfile
import unittest
import wave
from pathlib import Path
import numpy as np
import yaml
from funscripter.audio import extract_audio, pcm_values
from funscripter.media import sha256
from funscripter.models import AudioAnalysisConfig
from funscripter.project import analyze_project, create_project, reopen_project


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_audio(self, signal, rate=22050, name="input.wav"):
        path = self.root / name
        if signal.ndim == 1:
            signal = signal[:, None]
        with wave.open(str(path), "wb") as stream:
            stream.setparams((signal.shape[1], 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes((signal * 32767).astype("<i2").tobytes())
        return path

    def analyze(self, path, name="result", **parameters):
        output = self.root / name
        output.mkdir()
        evidence = extract_audio(path, output, AudioAnalysisConfig(**parameters), sha256(path))
        with np.load(output / "audio_features.npz", allow_pickle=False) as cache:
            values, columns = cache["features"].copy(), list(cache["columns"])
        self.assertFalse((output / "features-working.npy").exists())
        return evidence, values, columns

    def test_antiphase_stereo_does_not_cancel_and_band_energy(self):
        time = np.arange(22050) / 22050
        signal = 0.5 * np.sin(2 * np.pi * 1000 * time)
        path = self.write_audio(np.column_stack([signal, -signal]))
        evidence, values, columns = self.analyze(path)
        full = values[0, columns.index("rms")]
        mid = values[0, columns.index("mid_rms")]
        self.assertAlmostEqual(full, 0.5 / np.sqrt(2), places=3)
        self.assertAlmostEqual(mid, full, places=3)
        self.assertTrue(np.allclose(values[:, columns.index("channel_0_rms")],
                                    values[:, columns.index("channel_1_rms")]))
        self.assertEqual(evidence["optional_components"], {"vad": "disabled", "panns": "disabled"})

    def test_block_partition_and_whole_track_reference_invariance(self):
        rng = np.random.default_rng(123)
        signal = rng.normal(0, 0.1, (22050 + 173, 2))
        path = self.write_audio(signal)
        a, left, _ = self.analyze(path, "left", block_samples=1024)
        b, right, _ = self.analyze(path, "right", block_samples=65536)
        np.testing.assert_array_equal(left, right)
        self.assertEqual(a["relative_db_reference_rms"], b["relative_db_reference_rms"])
        self.assertEqual(a["sample_count"], len(signal))
        self.assertEqual(int(left[-1, 0] + left[-1, 1]), len(signal))
        self.assertTrue(np.all(np.diff(left[:, 0]) == a["hop_samples"]))

    def test_silence_and_constant_tone_do_not_force_period(self):
        for name, signal in [("silence", np.zeros(22050 * 7)),
                             ("tone", 0.2 * np.sin(2 * np.pi * 1000 * np.arange(22050 * 7) / 22050))]:
            path = self.write_audio(signal, name=name + ".wav")
            evidence, values, _ = self.analyze(path, name)
            self.assertTrue(np.all(np.isfinite(values)))
            first = evidence["periodicity"][0]["features"]["rms"]
            self.assertFalse(first["candidates"])
            self.assertEqual(first["status"], "low_energy" if name == "silence" else "weak_modulation")
            if name == "silence":
                self.assertFalse(evidence["onsets"])

    def test_known_amplitude_period_and_onset_times(self):
        rate = 22050
        time = np.arange(rate * 10) / rate
        envelope = (np.mod(time, 0.5) < 0.12).astype(float)
        path = self.write_audio(0.5 * envelope * np.sin(2 * np.pi * 1000 * time))
        evidence, _, _ = self.analyze(path)
        candidates = evidence["periodicity"][0]["features"]["rms"]["candidates"]
        self.assertTrue(any(abs(c["period_ms"] - 500) < 20 for c in candidates), candidates)
        self.assertGreater(len(evidence["onsets"]), 5)
        self.assertTrue(any(abs(onset["time_ms"] - 500) < 50 for onset in evidence["onsets"]))

    def test_nyquist_and_short_audio(self):
        path = self.write_audio(np.zeros(8000), rate=8000)
        output = self.root / "bad"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            extract_audio(path, output, AudioAnalysisConfig(), sha256(path))
        path = self.write_audio(np.zeros(100), name="short.wav")
        evidence, _, _ = self.analyze(path)
        self.assertEqual(evidence["periodicity"][0]["features"]["rms"]["status"], "insufficient_duration")

    def test_pcm_widths_and_configuration(self):
        np.testing.assert_allclose(pcm_values(bytes([0, 128, 255]), 1, 1).ravel(), [-1, 0, 127/128])
        raw = bytes([0, 0, 128, 255, 255, 127, 0, 0, 0])
        np.testing.assert_allclose(pcm_values(raw, 3, 1).ravel(), [-1, 8388607/8388608, 0])
        for options in [{"hop_ms": 50}, {"min_period_ms": 3000}, {"bands_hz": {"bad": (300.0, 40.0)}}]:
            with self.assertRaises(ValueError):
                AudioAnalysisConfig(**options)

    def test_analysis_transaction_keeps_source_project_unchanged(self):
        self.write_audio(np.zeros(22050), name="track.wav")
        (self.root / "track.lrc").write_text("[00:00.100]测试", encoding="utf-8")
        config = {"audio": "track.wav", "subtitles": {"path": "track.lrc"},
                  "ffmpeg": "nonexistent-ffmpeg-for-test"}
        (self.root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        project = self.root / "project"
        create_project(self.root / "config.yaml", project)
        before = (project / "project.json").read_bytes()
        manifest = analyze_project(project, self.root / "analysis")
        self.assertEqual((project / "project.json").read_bytes(), before)
        reopen_project(project)
        for name, expected in manifest["artifacts"].items():
            self.assertEqual(sha256(self.root / "analysis" / name), expected)
        with self.assertRaisesRegex(ValueError, "already exists"):
            analyze_project(project, self.root / "analysis")
        (self.root / "bad-profile.yaml").write_text("bands_hz: {high: [3000, 20000]}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            analyze_project(project, self.root / "failed-analysis", self.root / "bad-profile.yaml")
        self.assertFalse((self.root / "failed-analysis").exists())
        self.assertFalse(list(self.root.glob(".funscripter-analysis-*")))


if __name__ == "__main__":
    unittest.main()
