"""Neutral fixtures for progress, finite counts and confirmed time anchors."""
import copy
import unittest

from pydantic import ValidationError
from funscripter.fusion import build_plan
from funscripter.models import ControlDirective, CountdownProgress
from funscripter.semantic_contracts import validate_batch_v2
from funscripter.models import SemanticBatchV2
from test_semantic_v2 import event
import test_fusion as fixtures


class ControlTests(unittest.TestCase):
    setUp = fixtures.FusionTests.setUp
    create = fixtures.FusionTests.create

    def profile(self, **values):
        return fixtures.make_profile(initial_state={"paused": True}, action_defaults={
            "pace_tier": "medium", "amplitude_tier": "small", "motion_style": "rhythmic"}, **values)

    def note(self, index, kind, *, origin=0, basis="explicit", **values):
        source = [self.cues[index]]
        if origin != index:
            source.append(self.cues[origin])
        directive = ControlDirective(kind=kind, sequence_cue_id=self.cues[origin].cue_id, **values).model_dump()
        return {"cue_id": self.cues[index].cue_id, "kind": "state_continuation", "basis": basis,
            "summary": "neutral control fixture", "control": directive,
            "source_cue_ids": [c.cue_id for c in source],
            "source_evidence": [{"cue_id": c.cue_id, "quote": c.text_for_analysis} for c in source]}

    def progress(self, index, current, **values):
        return self.note(index, "countdown", progress={"start_value": 4, "end_value": 0, "current_value": current}, **values)

    def build(self, notes, events=None, profile=None):
        events = events if events is not None else [event(self.cues[0], intent="start")]
        processed = {row["cue_id"] for row in notes + events}
        semantic = {"schema_version": "1.4", "events": events, "context_notes": notes,
            "cue_assessments": [{"cue_id": c.cue_id, "status": "processed" if c.cue_id in processed else "no_new_information",
                                 "reason": "neutral fixture"} for c in self.cues], "failures": []}
        validate_batch_v2(SemanticBatchV2.model_validate({k:v for k,v in semantic.items() if k != "failures"}), self.cues)
        return build_plan(self.project, self.timeline, 10000, semantic, self.outline,
                          None, self.candidates, profile or self.profile())

    def test_progress_observed_without_curve_or_physiological_claim(self):
        plan, _, _, _ = self.build([self.progress(0, 4), self.progress(2, 2), self.progress(4, 0)])
        self.assertEqual([s["control"]["progress"]["normalized"] for s in plan["segments"]], [0, 0.5, 1])
        self.assertTrue(all(s["preset_id"] == "medium_small" for s in plan["segments"]))
        self.assertTrue(all(row["status"] == "observed" for row in plan["control_dispositions"]))

    def test_curve_does_not_override_pause_or_explicit_parameter_change(self):
        profile = self.profile(progress_curve=[{"at": 0.0, "preset_id": "medium_small"},
                                               {"at": 0.5, "preset_id": "slow_small"}])
        events = [event(self.cues[0], intent="start"), event(self.cues[1], intent="pause"),
                  event(self.cues[3], intent="resume"), event(self.cues[4], intent="increase_rate")]
        plan, _, _, _ = self.build([self.progress(0, 4), self.progress(2, 2), self.progress(5, 0)], events, profile)
        segments = {s["start_ms"]: s for s in plan["segments"]}
        self.assertEqual(segments[2000]["mode"], "hold_position")
        self.assertEqual(segments[3000]["preset_id"], "slow_small")
        self.assertEqual(segments[4000]["preset_id"], "fast_small")
        self.assertEqual(segments[5000]["preset_id"], "fast_small")

    def test_regression_needs_reset_and_inference_needs_opt_in(self):
        plan, _, _, _ = self.build([self.progress(0, 0), self.progress(1, 4), self.progress(2, 4, status="reset"),
                                   self.progress(3, 2, basis="inferred")])
        rows = plan["control_dispositions"]
        self.assertIn("progress_regressed_without_reset", rows[1]["reasons"])
        segments = {s["start_ms"]: s for s in plan["segments"]}
        self.assertFalse(segments[1000]["control"]["progress"]["execution_allowed"])
        self.assertTrue(segments[2000]["control"]["progress"]["execution_allowed"])
        self.assertFalse(segments[3000]["control"]["progress"]["execution_allowed"])

    def test_round_trips_stop_after_exact_count(self):
        plan, _, script, _ = self.build([self.note(0, "repetition", count=3, unit="round_trip")])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)
        self.assertEqual(plan["control_execution"][0]["status"], "completed")
        self.assertEqual([p["pos"] for p in script["actions"] if p["at"] >= 3600], [50, 50])

    def test_single_travels_and_pause_resume_do_not_double_count(self):
        plan, _, script, _ = self.build([self.note(0, "repetition", count=3, unit="single_travel")])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)
        self.assertEqual(script["actions"][-1]["pos"], 60)
        events = [event(self.cues[0], intent="start"), event(self.cues[1], intent="pause"), event(self.cues[3], intent="resume")]
        plan, _, script, _ = self.build([self.note(0, "repetition", count=3, unit="round_trip")], events)
        points = {p["at"]: p["pos"] for p in script["actions"]}
        self.assertEqual(points[1000], points[3000])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)
        self.assertIn(5600, points)
        self.assertEqual(points[5600], 50)

    def test_stop_before_completion_and_unknown_unit_are_not_false_success(self):
        events = [event(self.cues[0], intent="start"), event(self.cues[2], intent="pause")]
        plan, _, _, _ = self.build([self.note(0, "repetition", count=8, unit="round_trip")], events)
        self.assertEqual(plan["control_execution"][0]["status"], "incomplete")
        self.assertLess(plan["control_execution"][0]["completed"], 8)
        plan, _, script, _ = self.build([self.note(0, "repetition", count=3)])
        self.assertEqual(plan["control_dispositions"][0]["status"], "unresolved")
        self.assertTrue(all(p["pos"] == 50 for p in script["actions"]))

    def test_rhythm_needs_anchors_and_hits_supplied_endpoints(self):
        note = self.note(0, "rhythm_follow", unit="single_travel")
        plan, _, script, _ = self.build([note])
        self.assertEqual(plan["control_dispositions"][0]["status"], "unresolved")
        self.assertTrue(all(p["pos"] == 50 for p in script["actions"]))
        profile = self.profile(rhythm_anchors_ms={self.cues[0].cue_id: [700, 1700, 2500]})
        plan, _, script, _ = self.build([note], profile=profile)
        points = {p["at"]: p["pos"] for p in script["actions"]}
        self.assertEqual([points[t] for t in (700, 1700, 2500)], [60, 40, 60])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)

    def test_rhythm_pause_does_not_replay_missed_anchors(self):
        profile = self.profile(rhythm_anchors_ms={self.cues[0].cue_id: [700, 1700, 2500]})
        events = [event(self.cues[0], intent="start"), event(self.cues[1], intent="pause"), event(self.cues[3], intent="resume")]
        plan, _, script, _ = self.build([self.note(0, "rhythm_follow", unit="single_travel")], events, profile)
        self.assertEqual(plan["control_execution"][0]["completed"], 1)
        self.assertTrue(all(p["pos"] == script["actions"][-1]["pos"] for p in script["actions"] if p["at"] >= 1000))

    def test_round_trip_anchors_return_to_origin_and_constraints_still_apply(self):
        profile = self.profile(rhythm_anchors_ms={self.cues[0].cue_id: [1200, 2400, 3600]})
        plan, _, script, _ = self.build([self.note(0, "rhythm_follow", unit="round_trip")], profile=profile)
        points = {p["at"]: p["pos"] for p in script["actions"]}
        self.assertEqual([points[t] for t in (1200, 2400, 3600)], [50, 50, 50])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)
        self.project.config.device.max_speed_pos_per_sec = 1.0
        _, _, _, validation = self.build([self.note(0, "rhythm_follow", unit="round_trip")], profile=profile)
        self.assertIn("device_speed_limit", validation["constraint_errors"])

    def test_schema_rejects_bad_ranges_curves_and_unowned_sequence(self):
        for values in [{"start_value": 4, "end_value": 4, "current_value": 4},
                       {"start_value": 4, "end_value": 0, "current_value": 5}]:
            with self.assertRaises(ValidationError):
                CountdownProgress(**values)
        with self.assertRaises(ValidationError):
            self.profile(progress_curve=[{"at": 0.5, "preset_id": "slow_small"}])
        with self.assertRaises(ValidationError):
            self.profile(rhythm_anchors_ms={"c": [100, 100]})
        invalid = self.progress(0, 4)
        invalid["control"]["sequence_cue_id"] = self.cues[2].cue_id
        with self.assertRaisesRegex(ValueError, "sequence_requires_source"):
            self.build([invalid])

    def test_definitions_do_not_execute_and_continued_tasks_keep_their_count(self):
        definition = self.note(0, "repetition", count=3, unit="round_trip")
        definition["kind"] = "rule_definition"
        plan, _, _, _ = self.build([definition], events=[])
        self.assertEqual(plan["control_dispositions"][0]["status"], "definition_only")
        self.assertEqual(plan["control_execution"], [])
        plan, _, _, _ = self.build([definition])
        self.assertEqual(plan["control_execution"][0]["completed"], 3)
        events = [event(self.cues[0], intent="start"), event(self.cues[1], intent="pause"),
                  event(self.cues[3], intent="resume")]
        plan, _, _, _ = self.build([self.note(0, "repetition", count=3, unit="round_trip"),
                                   self.note(3, "repetition", count=3, unit="round_trip")], events)
        self.assertEqual(plan["control_dispositions"][1]["status"], "continued")
        self.assertEqual(len(plan["control_execution"]), 1)
        self.assertEqual(plan["control_execution"][0]["completed"], 3)

    def test_conflicting_controls_hold_and_new_start_exits_an_old_task(self):
        notes = [self.note(0, "repetition", count=3, unit="round_trip"),
                 self.note(0, "rhythm_follow", unit="single_travel")]
        plan, _, script, _ = self.build(notes)
        self.assertTrue(all(row["status"] == "conflict" for row in plan["control_dispositions"]))
        self.assertTrue(all(p["pos"] == 50 for p in script["actions"]))
        events = [event(self.cues[0], intent="start"), event(self.cues[5], intent="start")]
        plan, _, script, _ = self.build([self.note(0, "repetition", count=1, unit="round_trip")], events)
        points = [p["pos"] for p in script["actions"] if p["at"] > 5000]
        self.assertGreater(len(set(points)), 1)
        self.assertEqual(plan["control_execution"][0]["completed"], 1)
