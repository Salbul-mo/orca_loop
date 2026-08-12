from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker_runner import (
    EvidenceLog,
    PermissionObservationError,
    WorkerRunnerError,
    _load_job,
    run_job,
)


class EvidenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.logs = self.root / "logs"
        self.contract = self.root / "contract.md"
        self.contract.write_text("contract only", encoding="utf-8")
        self.output = self.root / "out" / "artifact.json"
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def job(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "profile_command": ["codex", "exec", "-"],
            "agent_cwd": str(self.root),
            "contract_path": str(self.contract),
            "output_path": str(self.output),
            "task_id": "task-1",
            "dispatch_id": "ctx-1",
            "coordinator_handle": "term-coordinator",
            "worker_handle": "term-worker",
            "orca_executable": "orca",
            "timeout_ms": 10_000,
            "preamble": "unused",
            "log_dir": str(self.logs),
            "step_id": "g0004-plan",
        }
        value.update(overrides)
        return value

    def record(self) -> dict[str, object]:
        path = self.logs / "step-g0004-plan.runner.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def stdout_log(self) -> str:
        return (self.logs / "step-g0004-plan.stdout.log").read_text(
            encoding="utf-8"
        )

    def stderr_log(self) -> str:
        return (self.logs / "step-g0004-plan.stderr.log").read_text(
            encoding="utf-8"
        )


class SuccessEvidenceTest(EvidenceTestCase):
    def test_success_persists_command_and_streams(self) -> None:
        process = SimpleNamespace(returncode=0)
        process.communicate = lambda input, timeout: (
            b'{"schema_version":1}\n',
            b"warning: slow\n",
        )
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send") as send:
                result = run_job(self.job())

        self.assertEqual("PASS", result["status"])
        send.assert_called_once()
        record = self.record()
        self.assertEqual("PASS", record["status"])
        self.assertEqual(0, record["exit_code"])
        self.assertFalse(record["timed_out"])
        self.assertEqual(["codex", "exec", "-"], record["command"])
        self.assertIn("schema_version", self.stdout_log())
        self.assertIn("warning: slow", self.stderr_log())


class FailureEvidenceTest(EvidenceTestCase):
    def test_process_execution_denied_has_typed_reason_code(self) -> None:
        with patch(
            "worker_runner.subprocess.Popen",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(PermissionObservationError) as raised:
                run_job(self.job())
        self.assertEqual("PROCESS_EXECUTION_DENIED", raised.exception.reason_code)

    def test_outbox_write_denied_has_typed_reason_code(self) -> None:
        process = SimpleNamespace(returncode=0)
        process.communicate = lambda input, timeout: (
            b'{"schema_version":1}\n',
            b"",
        )
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch(
                "worker_runner._write_atomic",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(PermissionObservationError) as raised:
                    run_job(self.job())
        self.assertEqual("OUTBOX_WRITE_DENIED", raised.exception.reason_code)

    def test_nonzero_exit_keeps_all_evidence(self) -> None:
        process = SimpleNamespace(returncode=1)
        process.communicate = lambda input, timeout: (
            b"partial output\n",
            b"error: unknown model 'sonnet5'\n",
        )
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send") as send:
                with self.assertRaisesRegex(WorkerRunnerError, "agent exited 1"):
                    run_job(self.job())

        send.assert_not_called()
        record = self.record()
        self.assertEqual("FAILED", record["status"])
        self.assertEqual(1, record["exit_code"])
        self.assertIn("unknown model", str(record["error"]))
        self.assertEqual("partial output\n", self.stdout_log())
        self.assertIn("sonnet5", self.stderr_log())

    def test_timeout_keeps_partial_output(self) -> None:
        class TimingOutProcess:
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("codex", 10)
                return b"half a plan", b"interrupted"

            def poll(self):
                return 1

        process = TimingOutProcess()
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send"):
                with self.assertRaisesRegex(WorkerRunnerError, "timed out"):
                    run_job(self.job())

        record = self.record()
        self.assertEqual("FAILED", record["status"])
        self.assertTrue(record["timed_out"])
        self.assertEqual("half a plan", self.stdout_log())
        self.assertEqual("interrupted", self.stderr_log())

    def test_unextractable_output_is_preserved(self) -> None:
        process = SimpleNamespace(returncode=0)
        process.communicate = lambda input, timeout: (
            b"I could not complete the task.\n",
            b"",
        )
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send"):
                with self.assertRaises(WorkerRunnerError):
                    run_job(self.job())

        record = self.record()
        self.assertEqual("FAILED", record["status"])
        self.assertEqual(0, record["exit_code"])
        self.assertIn("could not complete", self.stdout_log())

    def test_unwritable_log_dir_does_not_mask_agent_error(self) -> None:
        process = SimpleNamespace(returncode=1)
        process.communicate = lambda input, timeout: (b"", b"boom")
        blocked = self.root / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send"):
                with self.assertRaisesRegex(WorkerRunnerError, "agent exited 1"):
                    run_job(self.job(log_dir=str(blocked)))


class JobSchemaTest(EvidenceTestCase):
    def test_load_job_requires_evidence_fields(self) -> None:
        import base64

        value = self.job()
        value.pop("log_dir")
        encoded = base64.urlsafe_b64encode(
            json.dumps(value).encode("utf-8")
        ).decode("ascii")
        with self.assertRaisesRegex(WorkerRunnerError, "schema mismatch"):
            _load_job(encoded)

    def test_evidence_log_rejects_unsafe_step_id(self) -> None:
        self.assertIsNone(
            EvidenceLog.from_job(self.job(step_id="../escape"))
        )
        self.assertIsNone(EvidenceLog.from_job(self.job(step_id="")))
        self.assertIsNone(EvidenceLog.from_job(self.job(log_dir="")))

    def test_run_job_without_evidence_fields_still_runs(self) -> None:
        value = self.job()
        value.pop("log_dir")
        value.pop("step_id")
        process = SimpleNamespace(returncode=0)
        process.communicate = lambda input, timeout: (
            b'{"schema_version":1}\n',
            b"",
        )
        with patch("worker_runner.subprocess.Popen", return_value=process):
            with patch("worker_runner._send"):
                result = run_job(value)
        self.assertEqual("PASS", result["status"])
        self.assertFalse(self.logs.exists())


if __name__ == "__main__":
    unittest.main()
