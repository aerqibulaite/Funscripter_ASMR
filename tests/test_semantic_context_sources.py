"""Historical evidence stays available without rewriting the original script."""
import json
import tempfile
import unittest

from funscripter.models import LLMConfig, Outline, SemanticBatchV2
from funscripter.planner import Planner
from funscripter.semantic_contracts import validate_batch_v2
from test_context_planning import cue, note
from test_semantic_v2 import batch, event


class SemanticContextSourceTests(unittest.TestCase):
    def config(self):
        return LLMConfig(backend="ollama", base_url="http://localhost:1", model_id="fixture",
                         context_cues=0, max_chunk_cues=1, max_retries=0,
                         context_budget_tokens=65536)

    def outline(self, cues):
        return Outline(schema_version="1.3", phases=[{
            "phase_id": "p", "start_cue_id": cues[0].cue_id,
            "end_cue_id_inclusive": cues[-1].cue_id, "summary": "neutral fixture",
            "trend_hint": "unknown", "evidence_cue_ids": [cues[0].cue_id], "needs_review": True}])

    def test_sources_are_chronological_unique_and_exclude_targets(self):
        cues = [cue(f"c{i}", f"保留原文{i}") for i in range(4)]
        planner = Planner(self.config(), "unused")
        memory = [note(cues[0], "rule_definition", "explicit")]
        previous = [event(cues[2], sources=[cues[2], cues[0]])]
        result = planner.semantic_context(cues, [cues[3]], [cues[2], cues[1], cues[3]], memory, previous)
        self.assertEqual([c.cue_id for c in result], ["c0", "c1", "c2"])
        self.assertIs(result[0], cues[0])

    def test_cross_batch_request_keeps_original_sources_of_historical_events(self):
        cues = [cue("c0", "先暂停，等待后续指令。"), cue("c1", "这里是背景说明。"),
                cue("c2", "现在继续。")]
        seen = []

        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            seen.append(data)
            current = next(c for c in cues if c.cue_id == data["target_cues"][0]["cue_id"])
            events = ([event(current, intent="pause")] if current is cues[0] else
                      [event(current, sources=[cues[0], current], intent="continue")] if current is cues[2] else [])
            return batch([current], events).model_dump_json(), {}

        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(self.config(), directory, transport)
            events, failures = planner.semantic(cues, self.outline(cues))
        self.assertEqual(failures, [])
        self.assertEqual([e["intent"] for e in events], ["pause", "continue"])
        self.assertEqual(seen[2]["context_cues"][0]["text"], cues[0].text_for_analysis)
        self.assertEqual(events[-1]["source_evidence"][0]["quote"], cues[0].text_for_analysis)

    def test_budget_omission_removes_only_unneeded_history_sources(self):
        cues = [cue("c0", "原始规则与指令。"), cue("c1", "后续说明。")]
        for keep_rule in (False, True):
            with self.subTest(keep_rule=keep_rule), tempfile.TemporaryDirectory() as directory:
                seen = []

                def transport(config, messages, schema):
                    data = json.loads(messages[-1]["content"])
                    seen.append(data)
                    current = cues[0] if len(seen) == 1 else cues[1]
                    result = batch([current], [event(current)] if current is cues[0] else [])
                    if current is cues[0] and keep_rule:
                        result = SemanticBatchV2.model_validate({**result.model_dump(),
                            "context_notes": [note(current, "rule_definition", "explicit")]})
                    return result.model_dump_json(), {}

                planner = Planner(self.config(), directory, transport)
                original_fits = planner.fits
                planner.fits = lambda task, data, model: original_fits(task, data, model) and not data.get("previous_accepted_events")
                events, failures = planner.semantic(cues, self.outline(cues))
                self.assertEqual(failures, [])
                self.assertEqual(len(events), 1)  # Prompt pruning never deletes saved output.
                self.assertEqual(seen[1]["previous_events_omitted_for_budget"], 1)
                self.assertEqual([c["cue_id"] for c in seen[1]["context_cues"]], ["c0"] if keep_rule else [])
                self.assertEqual(len(seen[1]["scene_context"]), int(keep_rule))

    def test_out_of_order_control_notes_preserve_latest_script_state(self):
        cues = [cue("c0", "本轮从二开始。"), cue("c1", "现在是一。")]
        notes = []
        for c, current in zip(cues, (2, 1)):
            row = note(c, "state_continuation", "explicit")
            row["control"] = {"kind": "countdown", "sequence_cue_id": "c0",
                              "progress": {"start_value": 2, "end_value": 0, "current_value": current}}
            if c is not cues[0]:
                row["source_cue_ids"].append("c0")
                row["source_evidence"].append({"cue_id": "c0", "quote": cues[0].text_for_analysis})
            notes.append(row)
        result = SemanticBatchV2.model_validate({**batch(cues, statuses={"c0": "processed", "c1": "processed"}).model_dump(),
                                                "context_notes": list(reversed(notes))})
        validate_batch_v2(result, cues)
        self.assertEqual([n.cue_id for n in result.context_notes], ["c0", "c1"])
        planner = Planner(self.config(), "unused")
        planner.context_notes = [n.model_dump() for n in result.context_notes]
        retained = [n for n in planner.scene_memory() if n.get("control")]
        self.assertEqual(retained[-1]["control"]["progress"]["current_value"], 1)
        self.assertEqual(retained[-1]["source_evidence"][0]["quote"], cues[1].text_for_analysis)

    def test_same_cue_notes_keep_model_order_and_quotes_verbatim(self):
        c = cue("c0", "『止まらないで』という引用。")
        rows = [note(c, "rule_definition", "explicit"), note(c, "scene_inference", "inferred")]
        result = SemanticBatchV2.model_validate({**batch([c], statuses={"c0": "processed"}).model_dump(),
                                                "context_notes": rows})
        validate_batch_v2(result, [c])
        self.assertEqual([n.kind for n in result.context_notes], [r["kind"] for r in rows])
        self.assertEqual([n.source_evidence[0].quote for n in result.context_notes], [c.text_for_analysis] * 2)


if __name__ == "__main__":
    unittest.main()
