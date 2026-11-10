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

from funscripter.boundaries import (BoundaryProfile, VadEvidence, build_candidates,
    create_boundary_candidates, CandidateDocument)
from funscripter.cli import main
from funscripter.lrc import parse_lrc, candidate_document
from funscripter.media import sha256
from funscripter.models import SubtitleConfig
from funscripter.project import create_project, analyze_project, atomic_json
from funscripter.tasks import read_task


class BoundaryUnitTests(unittest.TestCase):
    def setUp(self):
        self.timeline = parse_lrc("[00:00.100]台词\n[00:03.800]结尾", 4000, SubtitleConfig(path="x"))
        self.matrix = np.zeros((400, 3), dtype="float64")
        self.matrix[:, 0] = np.arange(400) * 10
        self.matrix[:, 1] = np.minimum(40, 4000 - self.matrix[:, 0])
        self.matrix[20:100, 2] = 0.1
        self.matrix[160:230, 2] = 0.1
        self.columns = ["start_sample", "valid_samples", "mid_rms"]
        self.evidence = {"sample_rate": 1000, "hop_samples": 10, "frame_samples": 40,
            "sample_count": 4000, "input_sha256": "a" * 64, "cache_key": "test"}

    def build(self, vad=None, profile=None, base=None):
        return build_candidates(self.matrix, self.columns, self.timeline, 4000, profile or BoundaryProfile(),
            self.evidence, base or candidate_document(self.timeline), vad)

    def vad(self, **changes):
        return VadEvidence.model_validate({"schema_version": "1", "input_sha256": "a" * 64,
            "status": "available", "extractor": "TEST_ONLY", "version": "fixture-1",
            "model_sha256": "b" * 64, "config_sha256": "c" * 64,
            "spans": [{"start_ms": 200, "end_ms": 1000, "evidence_id": "v1"},
                      {"start_ms": 1600, "end_ms": 2300, "evidence_id": "v2"}], **changes})

    def test_internal_pause_retains_multiple_candidates_and_never_accepts(self):
        row = self.build().cues[0]
        self.assertEqual([c.time_ms for c in row.utterance_end_candidates], [1000, 2300])
        self.assertEqual(len(row.audio_candidate_groups), 2)
        self.assertIn("possible_internal_pause", row.review_reasons)
        self.assertIn("multiple_audio_end_candidates", row.review_reasons)
        self.assertIsNone(row.accepted_candidate)
        self.assertTrue(all(c.status == "unaccepted" and c.score_kind == "uncalibrated_rank"
                            for c in row.utterance_end_candidates))

    def test_short_pause_does_not_create_false_end_and_eof_is_not_implicit_end(self):
        self.matrix[110:160, 2] = 0.1
        self.assertEqual([c.time_ms for c in self.build().cues[0].utterance_end_candidates], [2300])
        self.matrix[:, 2] = 0.1
        result = self.build()
        self.assertFalse(result.cues[0].utterance_end_candidates)
        self.assertIn("activity_reaches_search_end:mid", result.cues[0].review_reasons)

    def test_weak_audio_is_relative_above_floor_and_insufficient_below_floor(self):
        self.matrix[:, 2] *= 0.01  # RMS 0.001, approximately -60 dBFS.
        self.assertEqual(len(self.build().cues[0].utterance_end_candidates), 2)
        self.matrix[:, 2] *= 0.001
        row = self.build().cues[0]
        self.assertFalse(row.utterance_end_candidates)
        self.assertIn("insufficient_band_activity:mid", row.review_reasons)

    def test_band_groups_do_not_drop_provenance_or_turn_scores_into_probability(self):
        self.matrix = np.column_stack([self.matrix, self.matrix[:, 2]])
        self.columns.append("high_rms")
        row = self.build(profile=BoundaryProfile(bands=["mid", "high"])).cues[0]
        self.assertEqual(len(row.utterance_end_candidates), 4)
        self.assertEqual([len(group) for group in row.audio_candidate_groups], [2, 2])
        self.assertEqual({c.evidence["band"] for c in row.utterance_end_candidates}, {"mid", "high"})

    def test_vad_endpoints_are_candidates_with_shared_audio_and_no_auto_adoption(self):
        row = self.build(self.vad()).cues[0]
        self.assertEqual(len(row.audio_candidate_groups), 2)
        self.assertEqual(len(row.utterance_end_candidates), 4)
        self.assertIsNone(row.accepted_candidate)
        vad = [c for c in row.utterance_end_candidates if c.method == "vad_end"]
        self.assertTrue(all(c.score is None and "vad_end_is_not_sentence_end" in c.quality_flags for c in vad))
        with self.assertRaisesRegex(ValueError, "audio/time mapping"):
            self.build(self.vad(input_sha256="d" * 64))
        with self.assertRaisesRegex(ValueError, "audio/time mapping"):
            self.build(self.vad(spans=[{"start_ms": 3900, "end_ms": 4100, "evidence_id": "outside"}]))

    def test_vad_missing_failed_empty_and_overlapping_intervals_are_distinct(self):
        self.assertEqual(self.build().components["vad"], "disabled")
        failed = self.build(self.vad(status="failed", spans=[]))
        self.assertEqual(failed.components["vad"], "failed")
        self.assertIn("vad_failed", failed.cues[0].review_reasons)
        empty = self.build(self.vad(spans=[]))
        self.assertIn("vad_returned_no_intervals_not_proof_of_silence", empty.cues[0].review_reasons)
        result = self.build(self.vad(spans=[{"start_ms": 200, "end_ms": 1000, "evidence_id": "one"},
            {"start_ms": 800, "end_ms": 1500, "evidence_id": "two"}]))
        self.assertTrue(any("overlapping_vad_intervals" in c.quality_flags for c in result.cues[0].utterance_end_candidates))

    def test_existing_human_confirmation_survives_machine_rebuild(self):
        base = candidate_document(self.timeline)
        base["cues"][0]["accepted_candidate"] = {"time_ms": 2400, "method": "manual_confirmation",
            "evidence_id": "human-1", "status": "human_boundary"}
        row = self.build(base=base).cues[0]
        self.assertEqual(row.accepted_candidate.time_ms, 2400)
        self.assertEqual(row.accepted_candidate.evidence_id, "human-1")
        self.assertEqual(base["cues"][0]["accepted_candidate"]["time_ms"], 2400)

    def test_anomalous_cues_and_bad_vad_contracts_are_explicit(self):
        self.timeline = parse_lrc("[00:00.100]甲\n[00:00.100]乙\n[00:09.000]越界", 4000, SubtitleConfig(path="x"))
        result = self.build()
        self.assertIn("simultaneous_cue_starts", result.cues[0].review_reasons)
        self.assertIn("invalid_or_outside_search_span", result.cues[-1].review_reasons)
        for changes in ({"model_sha256": None}, {"source_start_ms": 10}, {"status": "failed"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.vad(**changes)


class BoundaryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rate = 22050
        time = np.arange(rate * 4) / rate
        gate = ((time >= 0.2) & (time < 1.0)) | ((time >= 1.6) & (time < 2.3))
        samples = (5000 * gate * np.sin(2 * np.pi * 1000 * time)).astype("<i2")
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(samples.tobytes())
        (self.root / "track.lrc").write_text("[00:00.100]台词 / Translation\n[00:02.500]\n[00:03.800]结尾", encoding="utf-8")
        self.config = self.root / "input.yaml"
        self.config.write_text(yaml.safe_dump({"audio": "track.wav", "subtitles": {"path": "track.lrc"},
            "ffmpeg": "nonexistent-ffmpeg-for-test"}), encoding="utf-8")
        self.project, self.analysis, self.output = self.root / "project", self.root / "analysis", self.root / "boundaries"
        create_project(self.config, self.project)
        analyze_project(self.project, self.analysis)

    def test_actual_audio_stage_preserves_inputs_hints_and_uses_saved_features(self):
        before = {str(p): sha256(p) for directory in (self.project, self.analysis) for p in directory.rglob("*") if p.is_file()}
        with patch("funscripter.audio.extract_audio", side_effect=AssertionError("FFT must not run")):
            result = create_boundary_candidates(self.project, self.analysis, self.output)
        doc = CandidateDocument.model_validate_json((self.output / "utterance_candidates.json").read_text(encoding="utf-8"))
        row = doc.cues[0]
        drops = [c.time_ms for c in row.utterance_end_candidates if c.method == "band_energy_drop"]
        self.assertTrue(any(abs(time - 1000) <= 40 for time in drops), drops)
        self.assertTrue(any(abs(time - 2300) <= 40 for time in drops), drops)
        self.assertTrue(any(c.method == "empty_timestamp_hint" and c.time_ms == 2500 for c in row.utterance_end_candidates))
        self.assertIsNone(row.accepted_candidate)
        self.assertFalse(result["automatic_acceptance"])
        self.assertFalse(result["score_is_probability"])
        self.assertEqual(before, {name: sha256(name) for name in before})
        for name, expected in result["artifact_hashes"].items():
            self.assertEqual(sha256(self.output / name), expected)
        with self.assertRaisesRegex(ValueError, "new directory"):
            create_boundary_candidates(self.project, self.analysis, self.output)

    def test_mismatched_project_band_or_tampered_features_refuse_publication(self):
        other = self.root / "other-project"
        create_project(self.config, other)
        with self.assertRaisesRegex(ValueError, "revision mismatch"):
            create_boundary_candidates(other, self.analysis, self.output)
        profile = self.root / "profile.yaml"
        profile.write_text("bands: [missing]\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "band is missing"):
            create_boundary_candidates(self.project, self.analysis, self.output, profile)
        (self.analysis / "audio_features.npz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "source artifact changed"):
            create_boundary_candidates(self.project, self.analysis, self.output)
        self.assertFalse(self.output.exists())

    def test_rehashed_bad_time_grid_is_still_rejected(self):
        features = self.analysis / "audio_features.npz"
        with np.load(features, allow_pickle=False) as archive:
            values = {key: archive[key] for key in archive.files}
        values["features"][1, 0] += 1
        np.savez_compressed(features, **values)
        manifest_path = self.analysis / "analysis_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["audio_features.npz"] = sha256(features)
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "time grid"):
            create_boundary_candidates(self.project, self.analysis, self.output)

    def test_rehashed_unsupported_time_reference_is_not_silently_reinterpreted(self):
        path = self.analysis / "audio_evidence.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["frame_time_reference"] = "centered"
        atomic_json(path, evidence)
        manifest_path = self.analysis / "analysis_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["audio_evidence.json"] = sha256(path)
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "unsupported time reference"):
            create_boundary_candidates(self.project, self.analysis, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_batch_imports_vad_and_tracks_all_sources(self):
        project = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        vad = self.root / "vad.json"
        atomic_json(vad, {"schema_version": "1", "input_sha256": project["artifact_hashes"]["analysis_audio"],
            "status": "available", "extractor": "TEST_ONLY", "version": "fixture-1",
            "model_sha256": "a" * 64, "config_sha256": "b" * 64,
            "spans": [{"start_ms": 200, "end_ms": 1000, "evidence_id": "fixture-vad"}]})
        config = self.root / "batch.yaml"
        config.write_text(yaml.safe_dump({"jobs": [{"id": "ends", "command": "utterance-candidates",
            "arguments": {"project": "project", "analysis": "analysis", "vad_evidence": "vad.json", "output": "run/ends"}}]}), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["batch", "--config", str(config), "--output", str(self.root / "run")])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["counts"]["completed"], 1)
        task = read_task(self.root / "run/.batch/tasks/ends.json")
        self.assertIn(str(vad), task.input_hashes)
        self.assertEqual(task.result["components"]["vad"], "available")


if __name__ == "__main__":
    unittest.main()
