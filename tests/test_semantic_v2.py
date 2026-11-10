import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path

import yaml

from funscripter.fuse_project import fuse_project, load_semantics
from funscripter.fusion import build_plan
from funscripter.lrc import candidate_document
from funscripter.models import LLMConfig, Outline, SemanticBatchV2, FusionProfile
from funscripter.planner import Planner, validate_outline
from funscripter.planning_inputs import planning_inputs
from funscripter.project import create_project, plan_project, reopen_project
from funscripter.semantic_contracts import event_identity, validate_batch_v2


def assessment(cue, status="no_new_information"):
    return {"cue_id": cue.cue_id, "status": status, "reason": "neutral fixture"}


def event(cue, sources=None, **changes):
    sources = sources or [cue]
    return {"cue_id": cue.cue_id, "intent": "maintain", "scope": "current_section", "anchor": "cue_start",
            "pace_tier": "unknown", "amplitude_tier": "unknown", "motion_style": "unknown",
            "evidence": cue.text_for_analysis, "needs_review": True, "reason": "neutral fixture",
            "source_cue_ids": [source.cue_id for source in sources],
            "source_evidence": [{"cue_id": source.cue_id, "quote": source.text_for_analysis} for source in sources],
            "statement_kind": "instruction", **changes}


def batch(cues, events=(), statuses=None):
    statuses = statuses or {}
    anchors = {row["cue_id"] for row in events}
    return SemanticBatchV2(schema_version="1.4", events=list(events), cue_assessments=[
        assessment(cue, statuses.get(cue.cue_id, "processed" if cue.cue_id in anchors else "no_new_information")) for cue in cues])


class SemanticV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 6 * 2)
        self.subtitle = self.root / "track.srt"
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:00,000\n无效区间\n\n"
            "2\n00:00:00,000 --> 00:00:02,020\n保持设置\n\n"
            "3\n00:00:02,000 --> 00:00:04,000\n普通说明\n\n"
            "4\n00:00:04,000 --> 00:00:06,000\n继续说明\n", encoding="utf-8")
        self.config = {"audio": "track.wav", "subtitles": {"path": "track.srt"}, "ffmpeg": "missing-ffmpeg-test"}
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.project_dir = self.root / "project"
        create_project(self.config_path, self.project_dir)
        self.project, self.timeline, self.media, _ = reopen_project(self.project_dir)
        self.cues, self.view = planning_inputs(self.timeline, self.media["duration_ms"])
        self.profile_path = self.root / "llm.yaml"
        self.profile_path.write_text(yaml.safe_dump(LLMConfig(backend="ollama", base_url="http://127.0.0.1:1",
            model_id="neutral-fixture", max_retries=0).model_dump(mode="json")), encoding="utf-8")
        self.fusion_profile = FusionProfile(preset_version="neutral-fixture", presets={}, tier_map={},
            unknown_policy="hold_position", initial_state={"pace_tier": "slow", "amplitude_tier": "small", "motion_style": "rhythmic"})

    def transport(self, config, messages, schema):
        data = json.loads(messages[-1]["content"])
        self.assertNotIn("cue_000001", [cue["cue_id"] for cue in data.get("cues", data.get("target_cues", []))])
        if data["task"] == "semantic":
            result = {"schema_version": "1.4", "events": [], "cue_assessments": [
                {"cue_id": cue["cue_id"], "status": "no_new_information", "reason": "protocol fixture only"}
                for cue in data["target_cues"]]}
        else:
            cues = data["cues"]
            result = {"schema_version": "1.3", "phases": [{"phase_id": "p", "start_cue_id": cues[0]["cue_id"],
                "end_cue_id_inclusive": cues[-1]["cue_id"], "summary": "neutral", "trend_hint": "unknown",
                "evidence_cue_ids": [cues[0]["cue_id"]], "needs_review": True}]}
        return json.dumps(result, ensure_ascii=False), {"mock": True}

    def outline(self, split=False):
        groups = [self.cues[:1], self.cues[1:]] if split else [self.cues]
        result = Outline(schema_version="1.3", phases=[{"phase_id": str(index),
            "start_cue_id": group[0].cue_id, "end_cue_id_inclusive": group[-1].cue_id,
            "summary": "neutral", "trend_hint": "unknown", "evidence_cue_ids": [group[0].cue_id], "needs_review": True}
            for index, group in enumerate(groups)])
        validate_outline(result, self.cues)
        return result

    def build(self, semantic, split=False):
        return build_plan(self.project, self.timeline, self.media["duration_ms"], semantic,
            self.outline(split), None, candidate_document(self.timeline), self.fusion_profile)

    def document(self, result):
        return {"schema_version": "1.4", "events": [dict(row.model_dump(mode="json"),
                    event_id=event_identity(row.model_dump(mode="json"))) for row in result.events],
                "cue_assessments": [assessment(self.timeline.cues[0], "excluded"),
                                    *[row.model_dump(mode="json") for row in result.cue_assessments]], "failures": []}

    def test_invalid_inputs_excluded_without_modifying_timeline(self):
        self.assertEqual(len(self.timeline.cues), 4)
        self.assertEqual(len(self.cues), 3)
        self.assertEqual(self.view["excluded_count"], 1)
        self.assertIn("overlapping_subtitles", self.view["cues"][1]["quality_flags"])
        planner = Planner(LLMConfig(backend="ollama", base_url="http://127.0.0.1:1", model_id="fixture"), self.root)
        payload = planner.cue_data(self.cues[0])
        self.assertEqual(payload["end_ms"], 2020)
        self.assertEqual(payload["end_time_source"], "subtitle_display_end")

    def test_zero_multiple_and_cross_cue_events_validate(self):
        validate_batch_v2(batch(self.cues), self.cues)
        first = event(self.cues[0], self.cues[:2])
        second = event(self.cues[0], anchor="after_utterance", intent="pause")
        result = batch(self.cues, [first, second])
        validate_batch_v2(result, self.cues)
        self.assertEqual(len(result.events), 2)
        self.assertNotEqual(event_identity(first), event_identity(second))

    def test_context_evidence_allowed_but_context_anchor_not_owned(self):
        result = batch(self.cues[1:], [event(self.cues[1], self.cues[:2])])
        validate_batch_v2(result, self.cues[1:], self.cues[:1])
        unowned = SemanticBatchV2(schema_version="1.4", events=[event(self.cues[0])],
                    cue_assessments=[assessment(cue) for cue in self.cues[1:]])
        with self.assertRaisesRegex(ValueError, "anchor_not_owned"):
            validate_batch_v2(unowned, self.cues[1:], self.cues[:1])

    def test_fabricated_cross_cue_evidence_and_duplicate_events_rejected(self):
        row = event(self.cues[0], self.cues[:2])
        row["source_evidence"][1]["quote"] = "not in source"
        with self.assertRaisesRegex(ValueError, "source_evidence_not_in_source"):
            validate_batch_v2(batch(self.cues, [row]), self.cues)
        row = event(self.cues[0])
        with self.assertRaisesRegex(ValueError, "duplicate_event_identity"):
            validate_batch_v2(batch(self.cues, [row, copy.deepcopy(row)]), self.cues)

    def test_source_order_does_not_create_a_second_identity(self):
        row = event(self.cues[0], self.cues[:2])
        reordered = copy.deepcopy(row)
        reordered["source_cue_ids"].reverse()
        reordered["source_evidence"].reverse()
        self.assertEqual(event_identity(row), event_identity(reordered))
        with self.assertRaisesRegex(ValueError, "duplicate_event_identity"):
            validate_batch_v2(batch(self.cues, [row, reordered]), self.cues)

    def test_coverage_and_no_new_information_are_not_interchangeable(self):
        result = batch(self.cues)
        result.cue_assessments.pop()
        with self.assertRaisesRegex(ValueError, "assessment_coverage"):
            validate_batch_v2(result, self.cues)
        with self.assertRaisesRegex(ValueError, "without_event"):
            validate_batch_v2(batch(self.cues, statuses={self.cues[0].cue_id: "processed"}), self.cues)
        with self.assertRaisesRegex(ValueError, "conflicts_with_assessment"):
            validate_batch_v2(batch(self.cues, [event(self.cues[0])],
                statuses={self.cues[0].cue_id: "no_new_information"}), self.cues)

    def test_no_new_information_and_summary_boundaries_preserve_state(self):
        plan, _, _, _ = self.build(self.document(batch(self.cues)), split=True)
        self.assertEqual(plan["state_transitions"], [])
        self.assertTrue(all(row["state"]["pace_tier"] == "slow" for row in plan["segments"]))
        self.assertIn("overlapping_subtitle_chapter_boundary_clipped",
                      plan["segments"][0]["review_reasons"])
        self.assertEqual(plan["phases"][0]["end_ms"], 2000)

    def test_ambiguous_assessment_is_not_silent_continuation(self):
        result = batch(self.cues, statuses={self.cues[1].cue_id: "ambiguous"})
        plan, _, _, _ = self.build(self.document(result))
        self.assertEqual(plan["state_transitions"][0]["output_state"]["pace_tier"], "unknown")

    def test_question_or_conditional_cannot_apply_as_direct_instruction(self):
        for kind in ("question", "conditional", "quotation", "description"):
            result = batch(self.cues, [event(self.cues[0], intent="pause", statement_kind=kind)])
            validate_batch_v2(result, self.cues)
            plan, _, _, _ = self.build(self.document(result))
            self.assertEqual(plan["event_dispositions"][0]["status"], "unresolved")
            self.assertNotIn("effective_time_ms", plan["event_dispositions"][0])

    def test_real_pipeline_empty_events_resume_and_fusion_read(self):
        output = self.root / "plan"
        run = plan_project(self.project_dir, output, self.profile_path, transport=self.transport)
        self.assertEqual(run["event_count"], 0)
        self.assertEqual(run["excluded_cue_count"], 1)
        self.assertEqual(run["status"], "needs_review")
        before = (output / "semantic_events.json").read_bytes()
        plan_project(self.project_dir, output, self.profile_path, resume=True,
                     transport=lambda *args: self.fail("expected cached requests"))
        self.assertEqual(before, (output / "semantic_events.json").read_bytes())
        data, _, _ = load_semantics(output, self.project, self.timeline, self.media["duration_ms"])
        self.assertEqual(len(data["cue_assessments"]), 4)
        profile = self.root / "fusion.yaml"
        profile.write_text(yaml.safe_dump(self.fusion_profile.model_dump(mode="json")), encoding="utf-8")
        result = fuse_project(self.project_dir, output, self.root / "fused", profile)
        self.assertTrue(result["script_exported"])

    def test_all_invalid_input_does_not_call_model(self):
        self.subtitle.write_text("1\n00:00:00,000 --> 00:00:00,000\n无效\n", encoding="utf-8")
        project = self.root / "invalid-project"
        create_project(self.config_path, project)
        output = self.root / "invalid-plan"
        run = plan_project(project, output, self.profile_path, transport=lambda *args: self.fail("no eligible cues"))
        self.assertEqual(run["global_input_strategy"], "no_eligible_cues")
        self.assertEqual(run["excluded_cue_count"], 1)
        saved = json.loads((output / "semantic_events.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["cue_assessments"][0]["status"], "excluded")

    def test_nonempty_cross_cue_and_multiple_event_bundle_round_trip(self):
        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            if data["task"] != "semantic":
                return self.transport(config, messages, schema)
            rows = [event(self.cues[0], self.cues[:2]),
                    event(self.cues[0], intent="pause", anchor="after_utterance")]
            return batch(self.cues, rows).model_dump_json(), {"mock": True}
        output = self.root / "multi-plan"
        run = plan_project(self.project_dir, output, self.profile_path, transport=transport)
        self.assertEqual(run["event_count"], 2)
        data, outline, _ = load_semantics(output, self.project, self.timeline, self.media["duration_ms"])
        self.assertEqual(len(data["events"][0]["source_cue_ids"]), 2)
        plan, _, _, _ = build_plan(self.project, self.timeline, self.media["duration_ms"], data, outline,
            None, candidate_document(self.timeline), self.fusion_profile)
        self.assertNotIn("effective_time_ms", plan["event_dispositions"][1])

    def test_saved_bundle_rejects_excluded_sources(self):
        from funscripter.semantic_contracts import parse_saved_v2
        row = event(self.cues[0], [self.timeline.cues[0], self.cues[0]])
        saved = self.document(batch(self.cues, [row]))
        with self.assertRaisesRegex(ValueError, "excluded_source"):
            parse_saved_v2(saved, self.timeline.cues, {cue.cue_id for cue in self.cues})


if __name__ == "__main__":
    unittest.main()
