import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import yaml

from funscripter.cli import main, execute_stage
from funscripter.project import atomic_json
from funscripter.tasks import read_task, resume_task, run_task, task_lock, task_summary


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 22050 * 2)
        (self.root / "track.lrc").write_text("[00:00.1]准备", encoding="utf-8")
        self.config = self.root / "config.yaml"
        self.config.write_text(yaml.safe_dump({"audio": "track.wav", "subtitles": {"path": "track.lrc"},
            "ffmpeg": "nonexistent-ffmpeg-for-test"}), encoding="utf-8")
        self.task = self.root / "create-task.json"
        self.output = self.root / "project"
        self.arguments = {"config": str(self.config), "output": str(self.output)}

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def run_create(self, executor=execute_stage):
        with contextlib.redirect_stderr(io.StringIO()):
            return run_task("create", self.arguments, executor, self.task)

    def recover(self, executor=execute_stage):
        with contextlib.redirect_stderr(io.StringIO()):
            return resume_task(self.task, executor)

    def fail_create(self):
        def fail(*args):
            raise OSError("temporary failure")
        with self.assertRaisesRegex(OSError, "temporary failure"):
            self.run_create(fail)

    def test_cli_create_query_completed_recovery_does_not_execute_again(self):
        code, result, stderr = self.cli("create", "--config", self.config, "--output", self.output,
                                        "--task-file", self.task)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["review_status"], "needs_review")
        self.assertEqual(result["status"], "needs_review")
        before = (self.output / "project.json").read_bytes()
        recovered = self.recover(lambda *args: self.fail("completed task executed again"))
        self.assertEqual(result["task_id"], recovered["task_id"])
        code, record, _ = self.cli("task-status", self.task)
        self.assertEqual(code, 0)
        self.assertEqual(len(record["attempts"]), 1)
        self.assertEqual(before, (self.output / "project.json").read_bytes())

    def test_failure_is_durable_and_recovery_uses_absolute_paths(self):
        self.fail_create()
        record = task_summary(self.task)
        self.assertEqual(record["execution_status"], "failed")
        self.assertIn("temporary failure", record["error"])
        with patch("pathlib.Path.cwd", return_value=self.root.parent):
            result = self.recover()
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual([a["execution_status"] for a in read_task(self.task).attempts], ["failed", "completed"])

    def test_cancelled_stage_without_output_can_restart(self):
        def cancel(*args):
            raise KeyboardInterrupt()
        result = self.run_create(cancel)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(result["review_status"], "not_available")
        self.assertEqual(self.recover()["execution_status"], "completed")

    def test_config_media_or_code_changes_refuse_recovery(self):
        self.fail_create()
        for path in (self.config, self.root / "track.wav", self.root / "track.lrc"):
            before = path.read_bytes()
            path.write_bytes(before + b"\n")
            with self.assertRaisesRegex(ValueError, "inputs/config changed"):
                self.recover()
            path.write_bytes(before)
        with patch("funscripter.tasks.implementation_hashes", return_value={}):
            with self.assertRaisesRegex(ValueError, "implementation changed"):
                self.recover()
        self.assertEqual(len(read_task(self.task).attempts), 1)

    def test_completed_output_tampering_and_deletion_are_detected(self):
        self.run_create()
        script = self.output / "exports/track.funscript"
        original = script.read_bytes()
        script.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "output changed"):
            self.recover()
        script.write_bytes(original)
        script.unlink()
        with self.assertRaisesRegex(ValueError, "output changed"):
            self.recover()

    def test_failed_published_bundle_is_preserved(self):
        def failed(*args):
            self.output.mkdir()
            (self.output / "diagnostic.json").write_text("{}", encoding="utf-8")
            return {"status": "failed", "error": "constraint failure"}
        result = self.run_create(failed)
        self.assertEqual(result["execution_status"], "failed")
        with self.assertRaisesRegex(ValueError, "published output is preserved"):
            self.recover()
        code, record, _ = self.cli("task-status", self.task)
        self.assertEqual(code, 0)
        self.assertEqual(record["error"], "constraint failure")
        self.assertEqual((self.output / "diagnostic.json").read_text(), "{}")

    def test_unverified_crash_output_is_never_overwritten(self):
        self.fail_create()
        record = read_task(self.task)
        record.execution_status = "running"
        atomic_json(self.task, record.model_dump())
        self.output.mkdir()
        (self.output / "partial").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unverified output"):
            self.recover()
        self.assertEqual((self.output / "partial").read_text(), "keep")

    def test_process_lock_and_existing_record_prevent_duplicate_execution(self):
        self.fail_create()
        with task_lock(self.task):
            with self.assertRaisesRegex(ValueError, "already running"):
                self.recover()
        with self.assertRaisesRegex(ValueError, "task file already exists"):
            self.run_create()
        self.assertEqual(len(read_task(self.task).attempts), 1)

    def test_record_cannot_be_inside_input_or_output(self):
        with self.assertRaisesRegex(ValueError, "outside input and output"):
            run_task("create", self.arguments, execute_stage, self.output / "task.json")
        self.run_create()
        args = {"project": str(self.output), "profile": None, "output": str(self.root / "analysis")}
        with self.assertRaisesRegex(ValueError, "outside input and output"):
            run_task("analyze", args, execute_stage, self.output / "task.json")

        # Reject nested outputs before creating an output lock inside the source project.
        before = sorted(str(path) for path in self.output.rglob("*"))
        args["output"] = str(self.output / "nested-analysis")
        with self.assertRaisesRegex(ValueError, "outside input directories"):
            run_task("analyze", args, execute_stage, self.root / "analysis-task.json")
        self.assertEqual(before, sorted(str(path) for path in self.output.rglob("*")))

    def test_planning_checkpoint_resume_and_tampering(self):
        self.run_create()
        planning = self.root / "plan"
        record = self.root / "plan-task.json"
        args = {"project": str(self.output), "profile": None, "output": str(planning), "resume": False}
        def interrupted(command, arguments):
            self.assertFalse(arguments["resume"])
            planning.mkdir()
            atomic_json(planning / "accepted.json", {"checkpoint": "preserve"})
            return {"status": "cancelled"}
        def resumed(command, arguments):
            self.assertTrue(arguments["resume"])
            self.assertEqual(json.loads((planning / "accepted.json").read_text()), {"checkpoint": "preserve"})
            return {"status": "needs_review"}
        with contextlib.redirect_stderr(io.StringIO()):
            run_task("plan", args, interrupted, record)
            checkpoint = (planning / "accepted.json").read_bytes()
            (planning / "accepted.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "output changed"):
                resume_task(record, resumed)
            (planning / "accepted.json").write_bytes(checkpoint)
            result = resume_task(record, resumed)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(len(read_task(record).attempts), 2)

    def test_input_change_during_execution_is_not_completed(self):
        def changed(*args):
            result = execute_stage(*args)
            (self.root / "track.lrc").write_text("changed", encoding="utf-8")
            return result
        with self.assertRaisesRegex(ValueError, "changed during execution"):
            self.run_create(changed)
        self.assertEqual(read_task(self.task).execution_status, "failed")
        self.assertTrue((self.output / "project.json").exists())

    def test_real_planner_reuses_outline_after_cancelled_semantic_request(self):
        self.run_create()
        profile = self.root / "llm.yaml"
        profile.write_text(yaml.safe_dump({"backend": "ollama", "base_url": "http://127.0.0.1:1",
            "model_id": "test-only", "max_retries": 0}), encoding="utf-8")
        planning, record = self.root / "planning", self.root / "planning-task.json"
        calls = []
        def transport(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            calls.append(data["task"])
            if data["task"] == "semantic":
                raise KeyboardInterrupt()
            cue = data["cues"][0]["cue_id"]
            return json.dumps({"schema_version": "1.3", "phases": [{"phase_id": "p", "start_cue_id": cue,
                "end_cue_id_inclusive": cue, "summary": "test", "trend_hint": "unknown",
                "evidence_cue_ids": [cue], "needs_review": True}]}), {}
        with patch("funscripter.llm.chat", side_effect=transport):
            code, result, stderr = self.cli("plan", self.output, "--profile", profile,
                                            "--output", planning, "--task-file", record)
        self.assertEqual(code, 130, stderr)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(calls, ["outline", "semantic"])
        calls.clear()
        def resumed(config, messages, schema):
            data = json.loads(messages[-1]["content"])
            calls.append(data["task"])
            self.assertEqual(data["task"], "semantic", "outline must come from the accepted checkpoint")
            cue = data["target_cues"][0]["cue_id"]
            return json.dumps({"schema_version": "1.3", "events": [{"cue_id": cue, "intent": "unknown",
                "scope": "uncertain", "anchor": "uncertain", "pace_tier": "unknown",
                "amplitude_tier": "unknown", "motion_style": "unknown", "evidence": "",
                "needs_review": True, "reason": "mock response"}]}), {}
        with patch("funscripter.llm.chat", side_effect=resumed):
            code, result, stderr = self.cli("task-resume", record)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(calls, ["semantic"])

    def test_stale_running_task_without_output_restarts_and_closes_old_attempt(self):
        self.fail_create()
        task = read_task(self.task)
        task.execution_status = "running"
        task.attempts[-1]["execution_status"] = "running"
        atomic_json(self.task, task.model_dump())
        self.assertEqual(self.recover()["execution_status"], "completed")
        attempts = read_task(self.task).attempts
        self.assertEqual([a["execution_status"] for a in attempts], ["failed", "completed"])
        self.assertIn("without finalizing", attempts[0]["error"])

    def test_default_task_path_and_failed_recovery_exit_code(self):
        code, result, stderr = self.cli("create", "--config", self.config, "--output", self.output)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(Path(result["task_file"]).parent, self.root / ".funscripter-tasks")
        script = self.output / "exports/track.funscript"
        script.write_text("{}", encoding="utf-8")
        code, result, stderr = self.cli("task-resume", result["task_file"])
        self.assertEqual(code, 2)
        self.assertIn("output changed", stderr)
        self.assertEqual(script.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
