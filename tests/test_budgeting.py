import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from funscripter.llm import count_prompt_tokens
from funscripter.models import LLMConfig, Outline
from funscripter.planner import Planner, validate_outline
from test_planner import mock_transport


class BudgetTests(unittest.TestCase):
    def config(self, **values):
        return LLMConfig(backend="llama_cpp", base_url="http://localhost:8080/v1",
                         model_id="fixture", max_retries=0, **values)

    def test_rendered_prompt_uses_server_template_and_special_tokens(self):
        seen = []
        def open_request(request, **kwargs):
            seen.append((request.full_url, json.loads(request.data)))
            payload = {"prompt": "<chat>中文</chat>"} if len(seen) == 1 else {"tokens": [1, 2, 3]}
            return io.BytesIO(json.dumps(payload).encode())
        with patch("funscripter.llm.build_opener", return_value=SimpleNamespace(open=open_request)):
            count = count_prompt_tokens(self.config(thinking=False), [{"role": "user", "content": "中文"}], {})
        self.assertEqual(count, 3)
        self.assertEqual(seen[0][0], "http://localhost:8080/apply-template")
        self.assertEqual(seen[0][1]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(seen[1][1], {"content": "<chat>中文</chat>", "add_special": True, "parse_special": True})

    def test_tokenizer_error_does_not_silently_underestimate(self):
        opener = SimpleNamespace(open=lambda *a, **k: io.BytesIO(b'{"prompt": 5}'))
        with patch("funscripter.llm.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "template_invalid"):
                count_prompt_tokens(self.config(), [], {})

    def test_server_count_is_cached_and_output_budget_is_reserved(self):
        planner = Planner(self.config(token_count_method="server", context_budget_tokens=8192,
                                      max_output_tokens=4096, safety_tokens=1024), Path("unused"), mock_transport)
        with patch("funscripter.llm.count_prompt_tokens", return_value=4000) as count:
            for _ in range(2):
                self.assertFalse(planner.fits("outline", {"cues": [{"text": "中" * 2000}]}, Outline))
            self.assertEqual(count.call_count, 1)

    def test_merge_schema_requires_actual_phase_reduction(self):
        planner = Planner(self.config(), Path("unused"), mock_transport)
        _, schema = planner.messages("merge_outline", {"partial_outlines": [{"phases": [{}] * 4}, {"phases": [{}] * 4}]}, Outline)
        self.assertEqual(schema["properties"]["phases"]["maxItems"], 4)
        self.assertEqual(schema["$defs"]["Phase"]["properties"]["summary"]["maxLength"], 160)

    def test_overlong_summary_is_rejected_even_without_schema_enforcement(self):
        def transport(*args):
            return json.dumps({"schema_version": "1.3", "phases": [{"phase_id": "p", "start_cue_id": "c",
                "end_cue_id_inclusive": "c", "summary": "中" * 161, "trend_hint": "unknown",
                "evidence_cue_ids": ["c"], "needs_review": True}]}), {}
        with tempfile.TemporaryDirectory() as directory:
            planner = Planner(self.config(), Path(directory), transport)
            self.assertIsNone(planner.call("outline", {"cues": [{"cue_id": "c"}]}, Outline, lambda value: None))
            self.assertEqual(planner.trace[-1]["error"], "outline_compression_limits_exceeded")

    def test_retry_receives_uncovered_cue_feedback_and_saves_actual_request(self):
        seen = []
        def transport(config, messages, schema):
            payload = json.loads(messages[-1]["content"])
            seen.append(payload)
            last = "c1" if len(seen) == 1 else "c2"
            return json.dumps({"schema_version": "1.3", "phases": [{"phase_id": "p", "start_cue_id": "c1",
                "end_cue_id_inclusive": last, "summary": "中性示例", "trend_hint": "unknown",
                "evidence_cue_ids": ["c1"], "needs_review": True}]}), {}
        with tempfile.TemporaryDirectory() as directory:
            config = self.config().model_copy(update={"max_retries": 1})
            planner = Planner(config, Path(directory), transport)
            cues = [SimpleNamespace(cue_id=value) for value in ("c1", "c2")]
            result = planner.call("outline", {"cues": [{"cue_id": c.cue_id} for c in cues]}, Outline,
                                  lambda value: validate_outline(value, cues))
            self.assertEqual(result.phases[-1].end_cue_id_inclusive, "c2")
            self.assertIn("next_uncovered=c2", seen[1]["validation_feedback"]["error"])
            saved = list(Path(directory).glob("requests/*/request-2.json"))
            self.assertEqual(len(saved), 1)


if __name__ == "__main__":
    unittest.main()
