import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from funscripter.model_review import entries, inspect_entry, save_feedback, reanalyze, inside
from funscripter.gui_backend import load_model
from funscripter.models import LLMConfig
from funscripter.models import Outline
from funscripter.llm import chat, ModelResponseError
from funscripter.planner import Planner
from types import SimpleNamespace
import test_planner as fixtures


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "plan"
        self.folder = self.run / "requests" / ("a" * 64)
        self.folder.mkdir(parents=True)
        self.request = {"messages": [{"role": "system", "content": "fixture"}, {"role": "user", "content": json.dumps(
            {"task": "semantic", "semantic_contract_version": "1.4", "target_cues": [{"cue_id": "c1", "text": "中性台词"}],
             "context_cues": [], "global_outline": [], "previous_accepted_events": []})}], "schema": {}}
        (self.folder / "request.json").write_text(json.dumps(self.request), encoding="utf-8")
        config = LLMConfig(backend="ollama", base_url="http://localhost:11434", model_id="fixture", max_output_tokens=128)
        (self.run / "planning_run.json").write_text(json.dumps({"config": config.model_dump(mode="json")}), encoding="utf-8")

    def test_pending_response_then_reasoning_and_retry_status_are_visible(self):
        self.assertEqual(entries(self.run)[0]["status"], "等待回答")
        (self.folder / "response-1.json").write_text(json.dumps({"content": '{"x":1}', "metadata": {"thinking": "explanation"}}), encoding="utf-8")
        (self.run / "request_log.json").write_text(json.dumps([{"task": "semantic", "key": "a"*64, "attempt": 1, "status": "failed", "error": "fixture_error"}]), encoding="utf-8")
        row = entries(self.run)[0]
        self.assertEqual(row["error"], "fixture_error")
        detail = inspect_entry(self.run, row)
        self.assertEqual(detail["reasoning"], "explanation")
        self.assertIn('"x": 1', detail["answer"])
        self.assertIn("中性台词", detail["analysis"])
        self.assertIn("未返回逐句判断", detail["analysis"])

    def test_feedback_candidate_preserves_original_and_validates_output(self):
        row = entries(self.run)[0]
        before = (self.folder / "request.json").read_bytes()
        seen = []
        def transport(config, messages, schema):
            seen.append(json.loads(messages[-1]["content"]))
            return fixtures.mock_transport(config, messages, schema)
        folder = reanalyze(self.root, self.run, row, "请结合上下文复核", transport)
        self.assertTrue((folder / "candidate.json").exists())
        self.assertEqual(seen[0]["human_feedback"], "请结合上下文复核")
        self.assertEqual(before, (self.folder / "request.json").read_bytes())
        self.assertFalse((self.run / "semantic_events.json").exists())

    def test_path_escape_and_empty_feedback_rejected(self):
        with self.assertRaises(ValueError):
            inside(self.run, self.root / "other.json")
        with self.assertRaises(ValueError):
            save_feedback(self.root, self.run, entries(self.run)[0], " ")

    def test_invalid_candidate_is_saved_but_not_accepted(self):
        with self.assertRaises(ValueError):
            reanalyze(self.root, self.run, entries(self.run)[0], "复核", lambda *_: ('{}', {}))
        self.assertEqual(len(list(self.root.glob('.model-reviews/*/response-1.json'))), 1)
        self.assertFalse(list(self.root.glob('.model-reviews/*/candidate.json')))

    def test_adapter_preserves_service_reasoning(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": '{}', "reasoning_content": "service explanation"}}]}
        opener = SimpleNamespace(open=lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
        config = LLMConfig(backend="llama_cpp", base_url="http://localhost", model_id="fixture")
        with patch("funscripter.llm.build_opener", return_value=opener):
            _, metadata = chat(config, [], {})
        self.assertEqual(metadata["reasoning_content"], "service explanation")

    def test_incomplete_response_is_retained_for_inspection(self):
        def transport(*args):
            raise ModelResponseError("model_response_incomplete_or_refused", {"choices": []},
                                     {"content": '{"partial":', "reasoning_content": "partial explanation"})
        config = LLMConfig(backend="llama_cpp", base_url="http://localhost", model_id="fixture", max_retries=0)
        planner = Planner(config, self.root / "failed", transport)
        self.assertIsNone(planner.call("outline", {"cues": [{"cue_id": "c1"}]}, Outline, lambda _: None))
        files = list((self.root / "failed").glob('requests/*/response-1.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["content"], '{"partial":')
