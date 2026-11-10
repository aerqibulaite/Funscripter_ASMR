"""Neutral contextual fixtures: memory, evidence, pause/resume and editable knowledge."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from funscripter.fusion import transition
from funscripter.models import LLMConfig, Outline, SemanticBatchV2
from funscripter.model_profiles import load_model_profile
from funscripter.planner import Planner
from funscripter.project import plan_project
from funscripter.fuse_project import fuse_project
from funscripter.semantic_contracts import parse_saved_v2, validate_batch_v2
import test_fusion as fusion_fixtures
from test_fusion import make_profile
from test_planner import mock_transport
from test_semantic_v2 import event, batch


def cue(name, text):
    return SimpleNamespace(cue_id=name, text_for_analysis=text, start_ms=0, end_ms=1000,
                           end_inferred=False, speaker_label=None)


def note(c, kind="scene_inference", basis="inferred"):
    return {"cue_id": c.cue_id, "kind": kind, "basis": basis, "summary": "候选情境解释",
            "source_cue_ids": [c.cue_id], "source_evidence": [{"cue_id": c.cue_id, "quote": c.text_for_analysis}]}


class ContextTests(unittest.TestCase):
    def test_note_only_processed_and_saved_bundle_validate_without_action(self):
        c = cue("c1", "先讲规则，听到铃声就停")
        result = batch([c], statuses={c.cue_id: "processed"})
        result.context_notes = SemanticBatchV2.model_validate({**result.model_dump(),
            "context_notes": [note(c, "rule_definition", "explicit")]}).context_notes
        validate_batch_v2(result, [c])
        doc = {**result.model_dump(), "failures": []}
        self.assertEqual(parse_saved_v2(doc, [c], {c.cue_id}).events, [])
        bad = copy.deepcopy(doc)
        bad["context_notes"][0]["source_evidence"][0]["quote"] = "编造来源"
        with self.assertRaisesRegex(ValueError, "note_evidence"):
            parse_saved_v2(bad, [c], {c.cue_id})

    def test_rules_and_latest_phase_survive_long_description_without_rules_required(self):
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture")
        planner = Planner(config, "unused")
        self.assertEqual(planner.scene_memory(), [])
        rule = note(cue("r", "等待口令"), "rule_definition", "explicit")
        phase = note(cue("p", "现在开始"), "phase_transition", "explicit")
        planner.context_notes = [rule, phase] + [note(cue(str(i), "继续描述")) for i in range(12)]
        self.assertIn(rule, planner.scene_memory())
        self.assertIn(phase, planner.scene_memory())
        self.assertEqual(len(planner.scene_memory()), 8)

    def test_context_memory_and_original_evidence_reach_later_batches(self):
        cues = [cue("c1", "听到铃声暂停"), cue("c2", "先做一些说明"), cue("c3", "铃声到了")]
        seen = []
        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            seen.append(data)
            current = data["target_cues"][0]["cue_id"]
            c = next(c for c in cues if c.cue_id == current)
            result = batch([c])
            if current == "c1":
                result.cue_assessments[0].status = "processed"
                result.context_notes = SemanticBatchV2.model_validate({**result.model_dump(),
                    "context_notes": [note(c, "rule_definition", "explicit")]}).context_notes
            return result.model_dump_json(), {}
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture",
                           context_cues=0, max_chunk_cues=1, context_budget_tokens=32768)
        outline = Outline(schema_version="1.3", phases=[{"phase_id": "p", "start_cue_id": "c1",
            "end_cue_id_inclusive": "c3", "summary": "fixture", "trend_hint": "unknown",
            "evidence_cue_ids": ["c1"], "needs_review": True}])
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(config, directory, transport)
            events, failures = planner.semantic(cues, outline)
            self.assertEqual((events, failures), ([], []))
            self.assertEqual(seen[2]["scene_context"][0]["kind"], "rule_definition")
            self.assertEqual(seen[2]["context_cues"][0]["text"], cues[0].text_for_analysis)

    def test_pause_preserves_style_and_continue_resumes_but_maintain_does_not(self):
        c = cue("c", "中性动作")
        active = {"pace_tier": "fast", "amplitude_tier": "small", "motion_style": "rhythmic", "paused": False}
        paused, _ = transition(active, event(c, intent="pause", pace_tier="stop", motion_style="hold"), "hold_position")
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["motion_style"], "rhythmic")
        held, _ = transition(paused, event(c, intent="maintain"), "hold_position")
        self.assertTrue(held["paused"])
        resumed, reasons = transition(paused, event(c, intent="continue"), "hold_position")
        self.assertEqual(resumed, active)
        self.assertEqual(reasons, [])
        repeated, reasons = transition(active, event(c, intent="increase_rate", pace_tier="fast"), "hold_position")
        self.assertEqual(repeated, active)
        self.assertIn("absolute_target_already_reached", reasons)

    def test_knowledge_relative_path_content_hash_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "可以无规则直接进入活动；持续更久不等于更快"
            (root / "knowledge.md").write_text(text, encoding="utf-8")
            (root / "llm.yaml").write_text('backend: ollama\nbase_url: http://localhost:1\nmodel_id: fixture\n'
                'semantic_knowledge_file: knowledge.md\n', encoding="utf-8")
            config = load_model_profile(root / "llm.yaml")
            first = Planner(config, root)
            messages, _ = first.messages("outline", {}, Outline)
            self.assertIn(text, messages[0]["content"])
            (root / "knowledge.md").write_text(text + "。补充内容", encoding="utf-8")
            self.assertNotEqual(first.knowledge_hash, Planner(config, root).knowledge_hash)
            # Repair may use a frozen snapshot even if the original file has disappeared.
            (root / "knowledge.md").unlink()
            self.assertEqual(Planner(config, root, knowledge=text).knowledge_hash, first.knowledge_hash)


class ContextFusionTests(unittest.TestCase):
    setUp = fusion_fixtures.FusionTests.setUp
    create = fusion_fixtures.FusionTests.create
    build = fusion_fixtures.FusionTests.build

    def test_context_bundle_fuses_and_knowledge_change_invalidates_resume(self):
        import yaml
        knowledge = self.root / "knowledge.md"
        knowledge.write_text("活动可以没有规则说明", encoding="utf-8")
        profile = self.root / "llm.yaml"
        profile.write_text(yaml.safe_dump({"backend": "ollama", "base_url": "http://localhost:1",
            "model_id": "fixture", "semantic_knowledge_file": "knowledge.md"}), encoding="utf-8")
        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            content, metadata = mock_transport(config, messages, schema)
            if data["task"] == "semantic":
                response = json.loads(content)
                first = data["target_cues"][0]
                response["context_notes"] = [note(cue(first["cue_id"], first["text"]))]
                content = json.dumps(response, ensure_ascii=False)
            return content, metadata
        output = self.root / "context-plan"
        run = plan_project(self.project_dir, output, profile, transport=transport)
        self.assertEqual(run["failure_count"], 0)
        self.assertIn("semantic_knowledge.json", run["artifact_hashes"])
        fusion = self.root / "context-fusion.yaml"
        fusion.write_text(yaml.safe_dump(make_profile().model_dump()), encoding="utf-8")
        fused = self.root / "context-fused"
        fuse_project(self.project_dir, output, fused, fusion)
        result = json.loads((fused / "segment_plan.json").read_text("utf-8"))
        self.assertEqual(result["context_notes"][0]["kind"], "scene_inference")
        knowledge.write_text("知识已修改", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            plan_project(self.project_dir, output, profile, resume=True, transport=transport)


    def test_explicit_start_uses_defaults_only_when_needed(self):
        events = []
        for index, intent in [(1, "start"), (2, "pause"), (3, "continue"), (4, "increase_rate")]:
            row = event(self.cues[index], intent=intent)
            if intent == "pause":
                row.update(pace_tier="stop", motion_style="hold")
            events.append(row)
        profile = make_profile(initial_state={"paused": True},
            action_defaults={"pace_tier": "medium", "amplitude_tier": "small", "motion_style": "rhythmic"})
        plan, _, script, _ = self.build(profile=profile, events=events)
        states = {row["time_ms"]: row["output_state"] for row in plan["state_transitions"]}
        self.assertEqual(plan["segments"][0]["mode"], "hold_position")
        self.assertEqual(states[1000]["pace_tier"], "medium")
        self.assertTrue(states[2000]["paused"])
        self.assertFalse(states[3000]["paused"])
        self.assertEqual(states[4000]["pace_tier"], "fast")
        self.assertIn("configured_default_fields", plan["event_dispositions"][0])
        self.assertNotIn("configured_default_fields", plan["event_dispositions"][2])
        self.assertGreater(len(script["actions"]), 4)

    def test_same_cue_resume_and_maintain_combine_without_losing_sources(self):
        events = [event(self.cues[0], intent="start"),
            event(self.cues[1], intent="pause", motion_style="hold"),
            event(self.cues[2], intent="resume", motion_style="rhythmic"),
            event(self.cues[2], intent="maintain", pace_tier="medium")]
        profile = make_profile(initial_state={"paused": True},
            action_defaults={"pace_tier": "medium", "amplitude_tier": "small", "motion_style": "rhythmic"})
        plan, _, _, _ = self.build(profile=profile, events=events)
        final = plan["state_transitions"][-1]
        self.assertFalse(final["output_state"]["paused"])
        self.assertEqual(final["output_state"]["pace_tier"], "medium")
        self.assertIn("compatible_same_cue_events_combined", final["reasons"])
        self.assertEqual([row["status"] for row in plan["event_dispositions"][-2:]],
                         ["applied_to_draft", "applied_to_draft"])
        self.assertTrue(all(row["event_id"] in final["event_ids"] for row in plan["event_dispositions"][-2:]))
        # Opposite absolute fields and simultaneous independent actions stay conflicts.
        events[-2]["pace_tier"] = "fast"
        plan, _, _, _ = self.build(profile=profile, events=events)
        self.assertEqual(plan["event_dispositions"][-1]["status"], "conflict")
        events[-2]["pace_tier"] = "unknown"
        events[-1]["intent"] = "pause"
        plan, _, _, _ = self.build(profile=profile, events=events)
        self.assertEqual(plan["event_dispositions"][-1]["status"], "conflict")
