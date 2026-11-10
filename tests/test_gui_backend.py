import json
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from funscripter.gui_backend import DesktopController, load_model, probe_model, save_model
from funscripter.models import LLMConfig
from funscripter.tasks import task_summary


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = DesktopController(self.root / "results")
        self.audio = self.root / "音频.wav"
        with wave.open(str(self.audio), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 2)
        self.subtitles = self.root / "字幕.srt"
        self.subtitles.write_text("1\n00:00:00,100 --> 00:00:00,800\n测试文本", encoding="utf-8")

    def test_real_create_inspect_analyze_and_task_persistence(self):
        created = self.controller.create(str(self.audio), str(self.subtitles), "missing-ffmpeg-test", "utf-8", "0")
        project = created["output"]
        self.assertEqual(created["execution_status"], "completed")
        self.assertEqual(self.controller.inspect(project)["cue_count"], 1)
        analysis = self.controller.stage("analyze", project=project)
        self.assertEqual(analysis["execution_status"], "completed")
        self.assertTrue((Path(analysis["output"]) / "audio_evidence.json").is_file())
        self.assertEqual(task_summary(analysis["task_file"])["execution_status"], "completed")
        again = self.controller.create(str(self.audio), str(self.subtitles), "missing-ffmpeg-test", "utf-8", "0")
        self.assertNotEqual(again["output"], project)
        self.assertTrue(Path(project).is_dir())

    def test_failures_are_saved_and_invalid_inputs_do_not_create_projects(self):
        with self.assertRaises(ValueError):
            self.controller.create("missing.wav", str(self.subtitles), "ffmpeg", "utf-8", "0")
        created = self.controller.create(str(self.audio), str(self.subtitles), "missing-ffmpeg-test", "utf-8", "0")
        def fail(command, arguments):
            raise ValueError("synthetic worker failure")
        broken = DesktopController(self.root / "broken", executor=fail)
        with self.assertRaisesRegex(ValueError, "synthetic worker failure"):
            broken.stage("analyze", project=created["output"])
        tasks = list((self.root / "broken/.funscripter-tasks").glob("*.json"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(task_summary(tasks[0])["execution_status"], "failed")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            broken.stage("anything")

    def test_model_yaml_roundtrip_keeps_advanced_settings_and_no_secret_value(self):
        values = LLMConfig(backend="ollama", base_url="http://127.0.0.1:11434", model_id="local-test",
                           api_key_env="LOCAL_TEST_KEY", temperature=0.2, max_retries=1, thinking=False).model_dump(mode="json")
        path = self.root / "model.yaml"
        save_model(path, values)
        self.assertEqual(load_model(path).model_dump(mode="json"), values)
        with self.assertRaises(ValueError):
            save_model(path, {**values, "max_output_tokens": values["context_budget_tokens"]})
        self.assertEqual(load_model(path).model_dump(mode="json"), values)

    def test_connection_probe_exercises_both_real_http_adapters(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                message = {"content": '{"ok": true}'}
                result = {"done": True, "message": message} if self.path == "/api/chat" else {
                    "choices": [{"finish_reason": "stop", "message": message}]}
                raw = json.dumps(result).encode()
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
        for backend in ("ollama", "llama_cpp"):
            config = LLMConfig(backend=backend, base_url=f"http://127.0.0.1:{server.server_port}", model_id="mock").model_dump(mode="json")
            self.assertEqual(probe_model(config)["status"], "connected")
        self.assertEqual([path for path, _ in seen], ["/api/chat", "/v1/chat/completions"])
        self.assertEqual(seen[0][1]["options"]["num_predict"], 128)
        for content in ('{"ok": false}', '{"ok": 1}', 'not JSON'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                probe_model(config, lambda *_: (content, {}))


if __name__ == "__main__":
    unittest.main()
