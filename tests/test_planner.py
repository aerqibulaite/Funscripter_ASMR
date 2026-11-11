import json
import io
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import yaml
from funscripter.llm import chat, make_request
from funscripter.models import LLMConfig, Outline, SemanticBatch
from funscripter.planner import validate_events, validate_outline, Planner
from funscripter.project import create_project, plan_project, reopen_project


def profile(backend="llama_cpp", **extra):
    return LLMConfig(backend=backend, base_url="http://127.0.0.1:1", model_id="changeable-model",
                     max_retries=0, **extra)


def neutral_event(cue):
    return {"cue_id": cue["cue_id"], "intent": "unknown", "scope": "uncertain", "anchor": "uncertain",
        "pace_tier": "unknown", "amplitude_tier": "unknown", "motion_style": "unknown",
        "evidence": "", "needs_review": True, "reason": "模拟响应，仅验证协议，不验证语义准确性"}


def response_for(messages):
    data = json.loads(messages[-1]["content"])
    task = data["task"]
    if task == "semantic":
        result = {"schema_version": "1.3", "events": [neutral_event(cue) for cue in data["target_cues"]]}
        if data.get("semantic_contract_version") == "1.4":
            result["schema_version"] = "1.4"
            result["cue_assessments"] = [{"cue_id": cue["cue_id"], "status": "processed", "reason": "protocol fixture"}
                                          for cue in data["target_cues"]]
            for event, cue in zip(result["events"], data["target_cues"]):
                event.update(source_cue_ids=[cue["cue_id"]],
                             source_evidence=[{"cue_id": cue["cue_id"], "quote": cue["text"][:1]}],
                             statement_kind="uncertain")
    else:
        if task == "merge_outline":
            phases = [phase for partial in data["partial_outlines"] for phase in partial["phases"]]
            first, last = phases[0]["start_cue_id"], phases[-1]["end_cue_id_inclusive"]
        else:
            first, last = data["cues"][0]["cue_id"], data["cues"][-1]["cue_id"]
        result = {"schema_version": "1.3", "phases": [{"phase_id": "test_phase", "start_cue_id": first,
            "end_cue_id_inclusive": last, "summary": "中性示例", "trend_hint": "unknown",
            "evidence_cue_ids": [first], "needs_review": True}]}
    return json.dumps(result, ensure_ascii=False), {"model": "mock-only"}


def mock_transport(config, messages, schema):
    return response_for(messages)


class AdapterTests(unittest.TestCase):
    def test_local_http_protocols_and_response_extraction(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                content, _ = response_for(body["messages"])
                if self.path == "/api/chat":
                    payload = {"done": True, "done_reason": "stop", "message": {"content": content}}
                else:
                    payload = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for backend in ["llama_cpp", "ollama"]:
            config = LLMConfig(backend=backend, base_url=f"http://127.0.0.1:{server.server_port}",
                               model_id="switch-me", thinking=False)
            messages = [{"role": "user", "content": json.dumps({"task": "semantic", "target_cues": [{"cue_id": "c1"}]})}]
            content, _ = chat(config, messages, SemanticBatch.model_json_schema())
            self.assertEqual(SemanticBatch.model_validate_json(content).events[0].cue_id, "c1")
        self.assertEqual([item[0] for item in seen], ["/v1/chat/completions", "/api/chat"])
        self.assertEqual(seen[0][1]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIs(seen[1][1]["think"], False)
        self.assertIn("schema", seen[0][1]["response_format"])
        self.assertEqual(seen[1][1]["format"], SemanticBatch.model_json_schema())
        self.assertNotIn("temperature", seen[1][1]["options"])

    def test_empty_truncated_and_malformed_responses(self):
        payloads = [[], {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                    {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
                    {"choices": [{"finish_reason": "stop", "message": {"refusal": "refused", "content": "{}"}}]}]
        for payload in payloads:
            opener = SimpleNamespace(open=lambda *args, payload=payload, **kwargs: io.BytesIO(json.dumps(payload).encode()))
            with patch("funscripter.llm.build_opener", return_value=opener):
                with self.assertRaises(ValueError):
                    chat(profile(), [], {})
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
        with patch("funscripter.llm.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "connection_or_timeout"):
                chat(profile(), [], {})

    def test_configuration_and_backend_default(self):
        for url in ["ftp://localhost", "http://user:secret@localhost", "http://localhost?key=secret"]:
            with self.assertRaises(ValueError):
                LLMConfig(backend="llama_cpp", base_url=url, model_id="test")
        _, body = make_request(profile(), [], {})
        self.assertNotIn("chat_template_kwargs", body)
        self.assertEqual(body["model"], "changeable-model")

    def test_unavailable_service_reports_failure(self):
        with self.assertRaisesRegex(ValueError, "connection_or_timeout"):
            chat(profile(timeout_seconds=0.05), [], {})


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 2)
        self.configure_subtitles(["中性测试" for _ in range(8)])
        self.profile_path = self.root / "llm.yaml"
        self.profile_path.write_text(yaml.safe_dump(profile(max_chunk_cues=2).model_dump(mode="json")), encoding="utf-8")

    def configure_subtitles(self, texts):
        (self.root / "track.lrc").write_text("\n".join(f"[00:00.{i*100:03d}]{text}" for i, text in enumerate(texts)), encoding="utf-8")
        config = {"audio": "track.wav", "subtitles": {"path": "track.lrc"}, "ffmpeg": "nonexistent-ffmpeg-for-test"}
        (self.root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    def create(self):
        create_project(self.root / "config.yaml", self.root / "project")
        return self.root / "project"

    def test_full_two_pass_cue_ownership_resume_and_source_unchanged(self):
        project = self.create()
        before = (project / "project.json").read_bytes()
        calls = []
        def transport(config, messages, schema):
            calls.append(json.loads(messages[-1]["content"]))
            return mock_transport(config, messages, schema)
        output = self.root / "planning"
        run = plan_project(project, output, self.profile_path, transport=transport)
        self.assertEqual(run["status"], "needs_review")
        self.assertEqual(run["event_count"], 8)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(calls[1]["target_cues"]), 2)
        self.assertTrue(calls[2]["previous_accepted_events"])
        self.assertNotEqual(calls[1]["target_cues"][0]["cue_id"], calls[1]["context_cues"][0]["cue_id"])
        first = json.loads((output / "semantic_events.json").read_text(encoding="utf-8"))
        resumed = plan_project(project, output, self.profile_path, resume=True,
            transport=lambda *args: self.fail("accepted requests should be cached"))
        self.assertEqual(resumed["event_count"], 8)
        second = json.loads((output / "semantic_events.json").read_text(encoding="utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(len({event["event_id"] for event in first["events"]}), 8)
        self.assertEqual(before, (project / "project.json").read_bytes())
        reopen_project(project)
        with self.assertRaisesRegex(ValueError, "already exists"):
            plan_project(project, output, self.profile_path)

    def test_hierarchical_outline_when_full_text_exceeds_budget(self):
        self.configure_subtitles(["上下文" * 1000 for _ in range(8)])
        project = self.create()
        run = plan_project(project, self.root / "planning", self.profile_path, transport=mock_transport)
        self.assertEqual(run["status"], "needs_review", run)
        self.assertEqual(run["global_input_strategy"], "hierarchical_summaries")
        self.assertEqual(run["event_count"], 8)

    def test_invalid_json_retried_then_explicit_failures(self):
        project = self.create()
        config = profile(max_chunk_cues=2).model_dump(mode="json")
        config["max_retries"] = 1
        self.profile_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        calls = []
        def invalid(config, messages, schema):
            calls.append(1)
            return "not json", {}
        output = self.root / "planning"
        run = plan_project(project, output, self.profile_path, transport=invalid)
        self.assertEqual(len(calls), 2)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_count"], 8)
        self.assertEqual(run["event_count"], 0)

    def test_business_checks_reject_missing_ids_and_fabricated_evidence(self):
        project = self.create()
        _, timeline, _, _ = reopen_project(project)
        targets = timeline.cues[:2]
        with self.assertRaisesRegex(ValueError, "missing_duplicate"):
            validate_events(SemanticBatch.model_validate({"schema_version": "1.3", "events": []}), targets)
        rows = [neutral_event({"cue_id": cue.cue_id}) for cue in targets]
        rows[0].update(intent="pause", evidence="不存在的台词")
        with self.assertRaisesRegex(ValueError, "evidence_not_in_source"):
            validate_events(SemanticBatch.model_validate({"schema_version": "1.3", "events": rows}), targets)
        content, _ = response_for([{"content": json.dumps({"task": "outline", "cues": [{"cue_id": cue.cue_id} for cue in targets]})}])
        outline = Outline.model_validate_json(content)
        outline.phases[0].end_cue_id_inclusive = targets[0].cue_id
        with self.assertRaisesRegex(ValueError, "incomplete_coverage"):
            validate_outline(outline, targets)

    def test_failed_chunk_clears_previous_context_and_can_resume(self):
        project = self.create()
        seen = []
        def failing(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            if data["task"] == "semantic":
                seen.append(data)
                if data["target_cues"][0]["cue_id"] == "cue_000003":
                    raise ValueError("model_connection_or_timeout")
            return mock_transport(config, messages, schema)
        output = self.root / "planning"
        run = plan_project(project, output, self.profile_path, transport=failing)
        self.assertEqual(run["failure_count"], 2)
        self.assertEqual(seen[2]["previous_accepted_events"], [])
        run = plan_project(project, output, self.profile_path, resume=True, transport=mock_transport)
        self.assertEqual(run["event_count"], 8)
        self.assertEqual(run["failure_count"], 0)

    def test_invalid_multi_cue_response_subdivides_without_losing_ownership(self):
        project = self.create()
        sizes = []
        def transport(config, messages, schema):
            payload = json.loads(messages[-1]["content"])
            if payload["task"] == "semantic":
                sizes.append(len(payload["target_cues"]))
                if sizes[-1] > 1:
                    return "invalid JSON", {}
            return mock_transport(config, messages, schema)
        run = plan_project(project, self.root / "subdivided", self.profile_path, transport=transport)
        self.assertEqual(run["failure_count"], 0)
        self.assertEqual(run["event_count"], 8)
        data = json.loads((self.root / "subdivided/semantic_events.json").read_text("utf-8"))
        self.assertEqual(len({row["cue_id"] for row in data["cue_assessments"]}), 8)
        self.assertIn(1, sizes)
        self.assertIn(2, sizes)

    def test_cancelled_run_preserves_cache_and_resumes(self):
        project = self.create()
        def cancel(config, messages, schema):
            if json.loads(messages[-1]["content"])["task"] == "semantic":
                raise KeyboardInterrupt()
            return mock_transport(config, messages, schema)
        output = self.root / "planning"
        run = plan_project(project, output, self.profile_path, transport=cancel)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["failure_count"], 8)
        run = plan_project(project, output, self.profile_path, resume=True, transport=mock_transport)
        self.assertEqual(run["status"], "needs_review")
        data = yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))
        data["model_id"] = "changed-model"
        self.profile_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            plan_project(project, output, self.profile_path, resume=True, transport=mock_transport)

    def test_oversized_single_cue_is_not_truncated_or_sent(self):
        self.configure_subtitles(["长文本" * 40000])
        project = self.create()
        run = plan_project(project, self.root / "planning", self.profile_path,
            transport=lambda *args: self.fail("oversized request should not be sent"))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_count"], 1)


if __name__ == "__main__":
    unittest.main()
