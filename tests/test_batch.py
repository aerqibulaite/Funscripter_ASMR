import contextlib
import copy
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import yaml

from funscripter.batch import batch_status, resume_batch, run_batch
from funscripter.cli import execute_stage, main
from funscripter.project import atomic_json
from funscripter.tasks import read_task, task_lock


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with wave.open(str(self.root / "track.wav"), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0" * 44100)
        (self.root / "track.lrc").write_text("[00:00.100]合成测试", encoding="utf-8")
        (self.root / "input.yaml").write_text(yaml.safe_dump({"audio": "track.wav",
            "subtitles": {"path": "track.lrc"}, "ffmpeg": "nonexistent-ffmpeg-for-test"}), encoding="utf-8")
        self.config = self.root / "batch.yaml"
        self.output = self.root / "run"
        self.jobs = [
            {"id": "create", "command": "create", "arguments": {"config": "input.yaml", "output": "run/project"}},
            {"id": "analyze", "command": "analyze", "depends_on": ["create"],
             "arguments": {"project": "run/project", "output": "run/analysis"}},
            {"id": "reuse", "command": "analyze", "depends_on": ["analyze"],
             "arguments": {"project": "run/project", "reuse_analysis": "run/analysis", "output": "run/reused"}},
        ]

    def write_config(self, jobs=None, policy="stop"):
        self.config.write_text(yaml.safe_dump({"schema_version": "1", "failure_policy": policy,
            "jobs": self.jobs if jobs is None else jobs}), encoding="utf-8")

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def execute(self, executor=execute_stage):
        with contextlib.redirect_stderr(io.StringIO()):
            return run_batch(self.config, self.output, executor)

    def recover(self, executor=execute_stage):
        with contextlib.redirect_stderr(io.StringIO()):
            return resume_batch(self.output, executor)

    def task(self, name):
        return self.output / ".batch/tasks" / f"{name}.json"

    def test_cli_dependency_chain_cache_hit_and_resume_without_duplicate_work(self):
        self.write_config()
        code, result, stderr = self.cli("batch", "--config", self.config, "--output", self.output)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["counts"]["completed"], 3)
        self.assertEqual(result["items"][-1]["result"]["cache"]["status"], "hit")
        self.assertEqual(result["review_status"], "needs_review")
        recovered = self.recover(lambda *args: self.fail("completed stages must not rerun"))
        self.assertEqual(recovered["execution_status"], "completed")
        self.assertEqual(len(recovered["attempts"]), 2)
        for name in ("create", "analyze", "reuse"):
            self.assertEqual(len(read_task(self.task(name)).attempts), 1)
        code, record, _ = self.cli("batch-status", self.output)
        self.assertEqual(code, 0)
        self.assertEqual(record["counts"]["completed"], 3)

    def failure_jobs(self):
        jobs = copy.deepcopy(self.jobs[:2])
        jobs[0]["arguments"]["config"] = "initially-missing.yaml"
        jobs.append({"id": "independent", "command": "create",
            "arguments": {"config": "input.yaml", "output": "run/independent"}})
        return jobs

    def test_continue_skips_dependency_chain_but_finishes_independent_jobs(self):
        self.write_config(self.failure_jobs(), "continue")
        code, result, stderr = self.cli("batch", "--config", self.config, "--output", self.output)
        self.assertEqual(code, 2, stderr)
        self.assertEqual([x["execution_status"] for x in result["items"]], ["failed", "blocked", "completed"])
        self.assertFalse((self.output / "analysis").exists())
        self.assertEqual(self.cli("batch-status", self.output)[0], 0)
        # The failed preflight has not snapshotted this missing file yet.
        (self.root / "initially-missing.yaml").write_bytes((self.root / "input.yaml").read_bytes())
        result = self.recover()
        self.assertEqual(result["counts"]["completed"], 3)
        self.assertEqual(len(read_task(self.task("independent")).attempts), 1)

    def test_stop_preserves_pending_independent_job_for_later_resume(self):
        self.write_config(self.failure_jobs())
        result = self.execute()
        self.assertEqual([x["execution_status"] for x in result["items"]], ["failed", "blocked", "queued"])
        self.assertFalse((self.output / "independent").exists())
        (self.root / "initially-missing.yaml").write_bytes((self.root / "input.yaml").read_bytes())
        self.assertEqual(self.recover()["execution_status"], "completed")

    def test_cancel_stops_batch_and_recovers_unpublished_stage(self):
        self.write_config(policy="continue")
        def cancel(command, arguments):
            if command == "analyze":
                raise KeyboardInterrupt()
            return execute_stage(command, arguments)
        result = self.execute(cancel)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual([x["execution_status"] for x in result["items"]], ["completed", "cancelled", "blocked"])
        self.assertEqual(self.recover()["execution_status"], "completed")
        self.assertEqual(len(read_task(self.task("create")).attempts), 1)
        self.assertEqual(len(read_task(self.task("analyze")).attempts), 2)

    def test_tampered_output_blocks_downstream_and_is_not_overwritten(self):
        self.write_config()
        self.execute()
        script = self.output / "project/exports/track.funscript"
        script.write_text("tampered", encoding="utf-8")
        result = self.recover()
        self.assertEqual([x["execution_status"] for x in result["items"]], ["failed", "blocked", "blocked"])
        self.assertIn("output changed", result["items"][0]["error"])
        self.assertEqual(script.read_text(), "tampered")

    def test_batch_config_or_code_change_is_refused_before_new_attempt(self):
        self.write_config()
        self.execute()
        original = self.config.read_bytes()
        self.config.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.recover()
        self.config.write_bytes(original)
        with patch("funscripter.batch.implementation_hashes", return_value={}):
            with self.assertRaisesRegex(ValueError, "implementation changed"):
                self.recover()
        self.assertEqual(len(batch_status(self.output)["attempts"]), 1)

    def test_child_record_deletion_and_argument_substitution_are_not_adopted(self):
        self.write_config()
        self.execute()
        task_path = self.task("create")
        original = task_path.read_bytes()
        task_path.unlink()
        result = self.recover()
        self.assertIn("record missing", result["items"][0]["error"])
        task_path.write_bytes(original)
        record = json.loads(original)
        record["arguments"]["output"] = str(self.root / "unapproved-output")
        atomic_json(task_path, record)
        result = self.recover()
        self.assertIn("identity/arguments changed", result["items"][0]["error"])
        self.assertFalse((self.root / "unapproved-output").exists())

    def test_bad_child_record_stays_failed_without_crashing_continue_policy(self):
        self.write_config(policy="continue")
        self.execute()
        self.task("create").write_text("{}", encoding="utf-8")
        result = self.recover()
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["items"][0]["execution_status"], "failed")

    def test_batch_lock_and_existing_output_reject_duplicate_run(self):
        self.write_config()
        self.execute()
        with task_lock(self.output / ".batch/state.json"):
            with self.assertRaisesRegex(ValueError, "already running"):
                self.recover()
        with self.assertRaises(FileExistsError):
            self.execute()

    def test_recovery_after_lost_batch_progress_adopts_verified_child_completion(self):
        self.write_config()
        self.execute()
        state_path = self.output / ".batch/state.json"
        record = json.loads(state_path.read_text(encoding="utf-8"))
        record["execution_status"] = "running"
        record["attempts"][-1]["execution_status"] = "running"
        record["items"][0].update(execution_status="running", task_id=None, result=None)
        atomic_json(state_path, record)
        result = self.recover(lambda *args: self.fail("verified completed child must not rerun"))
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["attempts"][0]["execution_status"], "failed")
        self.assertEqual(len(read_task(self.task("create")).attempts), 1)

    def test_cli_cancel_returns_130_and_does_not_start_later_jobs(self):
        self.write_config()
        with patch("funscripter.cli.execute_stage", side_effect=KeyboardInterrupt()):
            code, result, stderr = self.cli("batch", "--config", self.config, "--output", self.output)
        self.assertEqual(code, 130, stderr)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertFalse((self.output / "project").exists())
        self.assertFalse(self.task("analyze").exists())

    def test_invalid_dependency_paths_commands_or_fields_have_no_side_effects(self):
        invalid = []
        jobs = copy.deepcopy(self.jobs)
        jobs[1]["depends_on"] = []
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["depends_on"] = ["reuse"]
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs)
        jobs[1]["id"] = "create"
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["command"] = "shell"
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["arguments"]["typo"] = True
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs)
        jobs[2]["arguments"]["output"] = "run/project/inside"
        invalid.append(jobs)
        jobs = copy.deepcopy(self.jobs[:1])
        jobs[0]["arguments"]["output"] = "run/.batch/inside"
        invalid.append(jobs)
        for jobs in invalid:
            with self.subTest(jobs=jobs):
                self.write_config(jobs)
                with self.assertRaises(ValueError):
                    self.execute(lambda *args: self.fail("invalid configuration executed"))
                self.assertFalse(self.output.exists())

    def test_published_failed_result_is_preserved_and_does_not_repeat(self):
        self.write_config(self.jobs[:1])
        def failed(command, arguments):
            output = Path(arguments["output"])
            output.mkdir()
            (output / "diagnostic.json").write_text("{}", encoding="utf-8")
            return {"status": "failed", "error": "constraint failure"}
        result = self.execute(failed)
        self.assertEqual(result["execution_status"], "failed")
        result = self.recover(lambda *args: self.fail("published failed bundle must not rerun"))
        self.assertIn("published output is preserved", result["items"][0]["error"])


if __name__ == "__main__":
    unittest.main()
