import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path
import numpy as np
import yaml
from PIL import Image
from funscripter.fusion import build_plan, transition, event_identity
from funscripter.fuse_project import fuse_project
from funscripter.models import FusionProfile, Outline, QualitativeState
from funscripter.project import create_project, reopen_project, atomic_json, analyze_project
from funscripter.media import sha256


def event(cue, **changes):
    return {"cue_id": cue.cue_id, "intent": "maintain", "scope": "current_section", "anchor": "cue_start",
        "pace_tier": "unknown", "amplitude_tier": "unknown", "motion_style": "unknown",
        "evidence": cue.text_raw, "needs_review": False, "reason": "测试输入", **changes}


def make_profile(**changes):
    raw = {"preset_version": "test-v1", "unknown_policy": "hold_position", "presets": {}, "tier_map": {}}
    for pace, period in [("slow", 2000), ("medium", 1200), ("fast", 700)]:
        key = pace + "_small"
        raw["presets"][key] = {"low": 40, "high": 60, "period_ms": period}
        raw["tier_map"][pace + "/small/rhythmic"] = key
    return FusionProfile.model_validate(raw | changes)


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((2, 2, 22050, 0, "NONE", "not compressed"))
            time = np.arange(22050 * 10) / 22050
            signal = (2000*np.sin(2*np.pi*500*time)).astype("<i2")
            stream.writeframes(np.column_stack([signal, -signal]).astype("<i2").tobytes())
        (self.root / "track.lrc").write_text("\n".join(f"[00:{i:02d}.000]中性测试{i}" for i in range(8)), encoding="utf-8")
        self.config = {"audio": "track.wav", "subtitles": {"path": "track.lrc"}, "ffmpeg": "missing-ffmpeg-test"}
        self.create()

    def create(self):
        self.project_dir = self.root / ("project" + str(len(list(self.root.glob("project*")))))
        (self.root / "config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")
        create_project(self.root / "config.yaml", self.project_dir)
        self.project, self.timeline, self.media, _ = reopen_project(self.project_dir)
        self.cues = self.timeline.cues
        self.candidates = json.loads((self.project_dir / "utterance_candidates.json").read_text(encoding="utf-8"))
        self.events = [event(cue) for cue in self.cues]
        self.events[0].update(pace_tier="slow", amplitude_tier="small", motion_style="rhythmic")
        self.outline = Outline.model_validate({"schema_version": "1.3", "phases": [{"phase_id": "phase1",
            "start_cue_id": self.cues[0].cue_id, "end_cue_id_inclusive": self.cues[-1].cue_id,
            "summary": "测试章节", "trend_hint": "unknown", "evidence_cue_ids": [self.cues[0].cue_id], "needs_review": True}]})

    def build(self, profile=None, audio=None, events=None):
        return build_plan(self.project, self.timeline, 10000,
            {"events": self.events if events is None else events, "failures": []},
            self.outline, audio, self.candidates, profile or make_profile())

    def publish_inputs(self, profile=None):
        planning = self.root / "planning"
        planning.mkdir(exist_ok=True)
        rows = [dict(row, event_id=event_identity(row), review_status="model_candidate_not_human_confirmed") for row in self.events]
        atomic_json(planning / "semantic_events.json", {"schema_version": "1.3", "events": rows, "failures": []})
        atomic_json(planning / "outline.json", self.outline.model_dump(mode="json"))
        atomic_json(planning / "planning_run.json", {"status": "needs_review", "project_id": self.project.project_id,
            "source_revision_id": self.project.revision_id, "subtitle_sha256": self.project.artifact_hashes["subtitles"],
            "artifact_hashes": {name: sha256(planning / name) for name in ["outline.json", "semantic_events.json"]}})
        profile_path = self.root / "fusion.yaml"
        profile_path.write_text(yaml.safe_dump((profile or make_profile()).model_dump(mode="json")), encoding="utf-8")
        return planning, profile_path

    def test_relative_absolute_pause_resume_and_unknown_initial_state(self):
        self.events[1].update(intent="increase_rate")
        self.events[2].update(intent="increase_rate", pace_tier="fast")
        self.events[3].update(intent="pause")
        self.events[4].update(intent="resume")
        self.events[5].update(intent="unknown", needs_review=True)
        self.events[6].update(intent="increase_rate")
        self.events[7].update(pace_tier="slow", amplitude_tier="small", motion_style="rhythmic")
        plan, _, script, validation = self.build()
        states = {row["time_ms"]: row["output_state"] for row in plan["state_transitions"]}
        self.assertEqual(states[1000]["pace_tier"], "medium")
        self.assertEqual(states[2000]["pace_tier"], "fast")
        self.assertTrue(states[3000]["paused"])
        self.assertFalse(states[4000]["paused"])
        self.assertEqual(states[6000]["pace_tier"], "unknown")
        points = {point["at"]: point["pos"] for point in script["actions"]}
        self.assertEqual(points[3000], points[4000])
        self.assertTrue(validation["format_valid"])
        self.assertFalse(validation["device_constraints_passed"])

    def test_resume_at_upper_endpoint_moves_instead_of_adding_a_hold_phase(self):
        self.events[1].update(intent="pause")
        self.events[2].update(intent="resume")
        _, _, script, _ = self.build()
        points = {point["at"]: point["pos"] for point in script["actions"]}
        self.assertEqual(points[1000], 60)
        self.assertEqual(points[2000], 60)
        self.assertEqual(points[3000], 40)

    def test_absolute_target_is_not_also_applied_as_relative_step(self):
        self.events[1].update(intent="increase_rate", pace_tier="medium")
        plan, _, _, _ = self.build()
        self.assertEqual(plan["state_transitions"][1]["output_state"]["pace_tier"], "medium")
        self.events[1].update(pace_tier="slow")
        plan, _, _, _ = self.build()
        self.assertEqual(plan["event_dispositions"][1]["status"], "conflict")

    def test_duplicate_event_is_not_applied_twice(self):
        self.events[1].update(intent="increase_rate")
        rows = self.events[:2] + [copy.deepcopy(self.events[1])] + self.events[2:]
        plan, _, _, _ = self.build(events=rows)
        self.assertEqual(plan["event_dispositions"][2]["status"], "duplicate_skipped")
        self.assertEqual(plan["state_transitions"][1]["output_state"]["pace_tier"], "medium")

    def test_utterance_boundary_is_not_next_cue_start_and_manual_boundary_splits_gap(self):
        self.events[1].update(intent="pause", anchor="after_utterance", needs_review=True)
        plan, _, _, _ = self.build()
        self.assertNotIn("effective_time_ms", plan["event_dispositions"][1])
        plan, candidates, script, _ = self.build(make_profile(manual_boundaries_ms={self.cues[1].cue_id: 1500}))
        self.assertEqual(plan["event_dispositions"][1]["effective_time_ms"], 1500)
        gap = next(segment for segment in plan["segments"] if segment["start_ms"] == 1500)
        self.assertEqual(gap["mode"], "hold_position")
        self.assertEqual(gap["subtitle_regions"][0]["region"], "inter_cue_gap")
        self.assertEqual(candidates["cues"][1]["accepted_candidate"]["status"], "human_boundary")

    def test_empty_hint_is_opt_in_inferred_and_ambiguity_is_not_adopted(self):
        self.events[1].update(intent="pause", anchor="after_utterance", needs_review=True)
        self.candidates["cues"][1]["utterance_end_candidates"] = [{"time_ms": 1500,
            "method": "empty_timestamp_hint", "evidence_id": "marker_test", "status": "unaccepted"}]
        profile = make_profile(use_empty_timestamp_hints=True, empty_timestamp_convention_confirmed=True)
        plan, candidates, _, _ = self.build(profile)
        self.assertEqual(plan["event_dispositions"][1]["anchor_source"], "inferred_draft_boundary")
        self.assertEqual(candidates["cues"][1]["accepted_candidate"]["status"], "inferred_draft_boundary")
        self.candidates["cues"][1]["utterance_end_candidates"].append({"time_ms": 1700,
            "method": "empty_timestamp_hint", "evidence_id": "marker_other", "status": "unaccepted"})
        plan, _, _, _ = self.build(profile)
        self.assertEqual(plan["event_dispositions"][1]["status"], "unresolved")

    def test_maintain_splits_do_not_reset_phase_or_delete_provenance(self):
        plan, _, script, _ = self.build()
        points = {point["at"]: point["pos"] for point in script["actions"]}
        self.assertEqual(points[1000], 60)
        self.assertEqual(points[2000], 40)
        self.assertGreater(len(plan["segments"][2]["event_ids"]), 1)
        self.assertEqual(plan["segments"][0]["start_ms"], 0)
        self.assertEqual(plan["segments"][-1]["end_ms"], 10000)
        self.assertTrue(all(a["end_ms"] == b["start_ms"] for a,b in zip(plan["segments"],plan["segments"][1:])))

    def test_audio_candidates_do_not_override_semantics_or_selected_preset(self):
        audio = {"periodicity": [{"start_ms": 0, "end_ms": 10000, "evidence_id": "period_test",
            "features": {"rms": {"candidates": [{"period_ms": 500.0, "score": 0.99}]}}}]}
        plan, _, _, _ = self.build(audio=audio)
        self.assertEqual(plan["segments"][0]["preset_id"], "slow_small")
        self.assertIn("audio_preset_period_difference_review", plan["segments"][0]["review_reasons"])
        self.assertEqual(plan["segments"][0]["audio_evidence_ids"], ["period_test"])

    def test_manual_presets_take_priority_and_locks_are_not_ignored(self):
        self.config["generation"] = {"presets": {"manual": {"low": 45, "high": 55, "period_ms": 4000}},
            "segments": [{"segment_id": "manual1", "start_ms": 1000, "end_ms": 3000, "preset_id": "manual"}]}
        self.create()
        plan, _, _, _ = self.build()
        chosen = next(row for row in plan["segments"] if row["start_ms"] == 1000)
        self.assertEqual(chosen["preset_id"], "manual")
        self.assertEqual(chosen["manual_source_id"], "manual1")
        # Altering a stored manual artifact is caught by source project hashes.
        (self.project_dir / "manual_overrides.json").write_text("{}", encoding="utf-8")
        planning, profile = self.publish_inputs()
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            fuse_project(self.project_dir, planning, self.root / "bad", profile)

    def test_publish_manifest_png_source_preservation_and_mismatch_rejection(self):
        planning, profile = self.publish_inputs(make_profile(preview_page_ms=4000))
        analysis = self.root / "analysis"
        analyze_project(self.project_dir, analysis)
        before = (self.project_dir / "project.json").read_bytes()
        output = self.root / "fusion"
        manifest = fuse_project(self.project_dir, planning, output, profile, analysis)
        self.assertTrue(manifest["script_exported"])
        self.assertEqual(before, (self.project_dir / "project.json").read_bytes())
        for name, expected in manifest["artifact_hashes"].items():
            self.assertEqual(sha256(output / name), expected)
        with Image.open(output / "preview.png") as image:
            self.assertEqual(image.size, (1600, 920))
        self.assertEqual(len(list((output / "preview_pages").glob("*.png"))), 3)
        with self.assertRaisesRegex(ValueError, "already exists"):
            fuse_project(self.project_dir, planning, output, profile)
        bad_manifest = json.loads((planning / "planning_run.json").read_text(encoding="utf-8"))
        bad_manifest["source_revision_id"] = "wrong"
        atomic_json(planning / "planning_run.json", bad_manifest)
        with self.assertRaisesRegex(ValueError, "revision mismatch"):
            fuse_project(self.project_dir, planning, self.root / "bad-fusion", profile)

    def test_device_constraint_failure_preserves_report_but_blocks_script(self):
        self.project.config.device.max_speed_pos_per_sec = 1.0
        plan, _, _, validation = self.build()
        self.assertIn("device_speed_limit", validation["constraint_errors"])
        # Persist a valid source device profile using initial hold generation only.
        self.config["device"] = {"allowed_position_range": [0,100], "max_speed_pos_per_sec": 1.0}
        self.create()
        planning, profile = self.publish_inputs()
        output = self.root / "blocked"
        manifest = fuse_project(self.project_dir, planning, output, profile)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse((output / "track.funscript").exists())
        self.assertTrue((output / "preview.png").exists())
        self.assertIn("device_speed_limit", json.loads((output / "validation.json").read_text())["constraint_errors"])

    def test_stored_locks_are_rejected_before_fusion(self):
        override_path = self.project_dir / "manual_overrides.json"
        atomic_json(override_path, {"schema_version": "1.3", "overrides": [], "locked_ranges": [[1000,2000]]})
        manifest = json.loads((self.project_dir / "project.json").read_text(encoding="utf-8"))
        manifest["artifact_hashes"]["overrides"] = sha256(override_path)
        atomic_json(self.project_dir / "project.json", manifest)
        planning, profile = self.publish_inputs()
        with self.assertRaisesRegex(ValueError, "manual curve edits/locks"):
            fuse_project(self.project_dir, planning, self.root / "locked", profile)
        self.assertFalse((self.root / "locked").exists())

    def test_hold_style_is_not_implicitly_resumed_as_periodic_motion(self):
        state = QualitativeState(pace_tier="slow", amplitude_tier="small", motion_style="hold").model_dump()
        result, reasons = transition(state, event(self.cues[0], intent="resume"), "hold_position")
        self.assertEqual(result["motion_style"], "hold")
        self.assertIn("resume_target_unknown", reasons)

    def test_chapter_reset_and_next_section_scope(self):
        phases = self.outline.model_dump(mode="json")["phases"]
        phases[0]["end_cue_id_inclusive"] = self.cues[3].cue_id
        phases.append({**phases[0], "phase_id": "phase2", "start_cue_id": self.cues[4].cue_id,
            "end_cue_id_inclusive": self.cues[7].cue_id, "evidence_cue_ids": [self.cues[4].cue_id]})
        self.outline = Outline.model_validate({"schema_version": "1.3", "phases": phases})
        self.events[2].update(scope="next_section", pace_tier="fast", amplitude_tier="small", motion_style="rhythmic")
        self.events[4].update(intent="unknown", scope="current_section", needs_review=True)
        plan, _, _, _ = self.build()
        record = plan["event_dispositions"][2]
        self.assertEqual(record["effective_time_ms"], 4000)
        segment = next(row for row in plan["segments"] if row["start_ms"] == 4000)
        self.assertEqual(segment["preset_id"], "fast_small")
        self.assertIn("unknown_context_at_explicit_event", segment["review_reasons"])


if __name__ == "__main__":
    unittest.main()
