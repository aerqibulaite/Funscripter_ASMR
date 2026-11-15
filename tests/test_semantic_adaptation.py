"""Original-language evidence, mechanical normalization and prefix recovery."""
import json
import tempfile
import unittest
from pathlib import Path

from funscripter.models import LLMConfig, Outline
from funscripter.planner import Planner
from funscripter.model_review import entries, inspect_entry
from funscripter.semantic_contracts import normalize_batch_v2, validate_batch_v2, validated_prefix_v2
from test_context_planning import cue
from test_semantic_v2 import batch, event


class SemanticAdaptationTests(unittest.TestCase):
    def test_review_metadata_is_completed_without_changing_intent_or_quote(self):
        c = cue("c1", "そのままです。")
        result = batch([c], [event(c, statement_kind="description", needs_review=False, reason="")])
        before = result.events[0].model_dump()
        changes = normalize_batch_v2(result)
        validate_batch_v2(result, [c])
        after = result.events[0].model_dump()
        for key in before.keys() - {"needs_review", "reason"}:
            self.assertEqual(before[key], after[key])
        self.assertTrue(after["needs_review"])
        self.assertIn("原模型未提供理由", after["reason"])
        self.assertEqual(changes[0]["kind"], "review_metadata_completed")
        self.assertEqual(normalize_batch_v2(result), [])

    def test_orphan_processed_is_reviewable_without_inventing_an_action(self):
        c = cue("c1", "準備してください。")
        result = batch([c], statuses={"c1": "processed"})
        normalize_batch_v2(result)
        validate_batch_v2(result, [c])
        self.assertEqual(result.events, [])
        self.assertEqual(result.cue_assessments[0].status, "ambiguous")

    def test_translated_evidence_is_still_rejected_with_target_id(self):
        c = cue("c1", "動かしてください。")
        result = batch([c], [event(c, evidence="开始移动")])
        normalize_batch_v2(result)
        with self.assertRaisesRegex(ValueError, "semantic_evidence_not_in_source_cue: event=c1"):
            validate_batch_v2(result, [c])

    def test_prefix_never_skips_an_invalid_cue_or_accepts_extra_anchors(self):
        cues = [cue(f"c{i}", f"原文{i}") for i in range(3)]
        result = batch(cues, [event(c, evidence="不存在" if i == 1 else c.text_for_analysis)
                              for i, c in enumerate(cues)])
        prefix = validated_prefix_v2(result, cues, [])
        self.assertEqual([x.cue_id for x in prefix.cue_assessments], ["c0"])
        result.events[0].evidence = "不存在"
        self.assertIsNone(validated_prefix_v2(result, cues, []))
        result.events[0].cue_id = "unowned"
        self.assertIsNone(validated_prefix_v2(result, cues, []))

    def test_planner_retains_good_prefix_and_requests_only_remaining_targets(self):
        cues = [cue(f"c{i}", f"動かして{i}") for i in range(3)]
        seen = []
        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            ids = [row["cue_id"] for row in data["target_cues"]]
            seen.append(ids)
            owned = [c for c in cues if c.cue_id in ids]
            result = batch(owned, [event(c, evidence="翻译后的证据" if len(seen) == 1 and c.cue_id == "c1"
                                        else c.text_for_analysis) for c in owned])
            return result.model_dump_json(), {}
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture",
                           context_cues=0, max_chunk_cues=3, max_retries=0)
        outline = Outline(schema_version="1.3", phases=[{"phase_id": "p", "start_cue_id": "c0",
            "end_cue_id_inclusive": "c2", "summary": "neutral", "trend_hint": "unknown",
            "evidence_cue_ids": ["c0"], "needs_review": True}])
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(config, directory, transport)
            events, failures = planner.semantic(cues, outline)
            self.assertEqual(failures, [])
            self.assertEqual([x["cue_id"] for x in events], ["c0", "c1", "c2"])
            self.assertEqual(seen, [["c0", "c1", "c2"], ["c1", "c2"]])
            self.assertEqual(planner.trace[0]["status"], "accepted_partial")
            self.assertTrue(list(Path(directory).glob("requests/*/partial.json")))
            inspected = next(row for row in entries(directory) if row["status"] == "accepted_partial")
            self.assertEqual(inspected["accepted_cue_ids"], ["c0"])
            self.assertEqual(json.loads(inspect_entry(directory, inspected)["metadata"])
                             ["program_validation"]["accepted_cue_ids"], ["c0"])

    def test_normalization_is_audited_and_original_answer_is_retained(self):
        c = cue("c1", "そのままです。")
        def transport(config, messages, schema):
            return batch([c], [event(c, statement_kind="description", needs_review=False)]).model_dump_json(), {}
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture")
        data = {"semantic_contract_version": "1.4", "target_cues": [{"cue_id": "c1", "text": c.text_for_analysis}],
                "context_cues": []}
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(config, directory, transport)
            result = planner.call("semantic", data, type(batch([c])), lambda value: validate_batch_v2(value, [c]))
            self.assertTrue(result.events[0].needs_review)
            entry = entries(directory)[0]
            raw = json.loads(json.loads(Path(entry["response"]).read_text(encoding="utf-8"))["content"])
            self.assertFalse(raw["events"][0]["needs_review"])
            metadata = json.loads(inspect_entry(directory, entry)["metadata"])
            self.assertEqual(metadata["program_validation"]["normalizations"][0]["kind"], "review_metadata_completed")

    def test_original_language_prompt_has_no_chinese_translation_assumption(self):
        config = LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture")
        planner = Planner(config, ".", input_kind="whisper")
        self.assertIn("自动语音识别", planner.system)
        self.assertNotIn("字幕是中文译文", planner.system)
        self.assertIn("止めないで", planner.system)
        self.assertIn("準備して", planner.system)


if __name__ == "__main__":
    unittest.main()
