from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orca_loop.dispatcher import (
    dispatch_and_wait,
    prepare_task,
    provision_workers,
)
from orca_loop.models import (
    CompletionKind,
    LaunchProfile,
    PreparedTask,
    RenderedContract,
    Role,
    StepStage,
    WorkerHandle,
    WorkerKey,
)
from orca_loop.workspace import create_run_workspace
from tests.fakes import FakeOrcaClient
from worker_runner import _send, extract_agent_artifact, run_job


DIGEST_A = "sha256:" + "a" * 64


class DispatcherTest(unittest.TestCase):
    def test_provision_creates_four_unique_shell_workers(self) -> None:
        counter = 0

        def handler(
            argv: tuple[str, ...],
            timeout_ms: int,
        ) -> dict[str, object]:
            nonlocal counter
            if argv[:2] == ("terminal", "create"):
                counter += 1
                return {
                    "terminal": {
                        "handle": f"term-{counter}",
                        "tabId": f"tab-{counter}",
                        "leafId": f"leaf-{counter}",
                        "worktreeId": "wt-1",
                    }
                }
            if argv[:2] == ("terminal", "show"):
                return {"terminal": {"status": "running"}}
            self.fail(f"unexpected call: {argv}")

        client = FakeOrcaClient(handler)
        profile = LaunchProfile(
            ("agent", "--add-dir", str(Path.cwd())),
            (),
            DIGEST_A,
        )
        profiles = {key: profile for key in WorkerKey}
        pool = provision_workers(
            client,  # type: ignore[arg-type]
            "id:repo::C:\\repo",
            profiles,
            coordinator_handle="term-coordinator",
        )
        self.assertEqual(4, len(pool.workers))
        self.assertEqual(
            4,
            len({item.terminal_handle for item in pool.workers}),
        )

    def test_prepare_commits_before_and_after_task_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, step = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            stages: list[StepStage] = []

            def handler(
                argv: tuple[str, ...],
                timeout_ms: int,
            ) -> dict[str, object]:
                self.assertTrue(
                    (step.input_dir / "contract.md").is_file()
                )
                self.assertIn(
                    StepStage.STEP_PREPARED,
                    stages,
                )
                return {"task": {"id": "task-1"}}

            worker = WorkerHandle(
                WorkerKey.CODEX_REVIEW,
                "term-1",
                "wt-1",
                "tab-1",
                "leaf-1",
            )
            prepared, manifest = prepare_task(
                FakeOrcaClient(handler),  # type: ignore[arg-type]
                step,
                RenderedContract("contract", DIGEST_A),
                worker,
                Role.PLAN_REVIEWER,
                additional_inputs=(),
                commit_stage=lambda stage, active: stages.append(stage),
            )
            self.assertEqual("task-1", prepared.task_id)
            self.assertEqual(
                [
                    StepStage.STEP_PREPARED,
                    StepStage.TASK_CREATED,
                ],
                stages,
            )
            self.assertTrue(manifest.entries)

    def test_dispatch_ignores_foreign_message_and_returns_exact_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, step = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            (step.input_dir / "contract.md").write_text(
                "contract",
                encoding="utf-8",
            )
            worker = WorkerHandle(
                WorkerKey.CODEX_REVIEW,
                "term-1",
                "wt-1",
                "tab-1",
                "leaf-1",
            )
            prepared = PreparedTask(
                "step-1",
                "task-1",
                worker,
                Role.PLAN_REVIEWER,
                DIGEST_A,
            )
            (step.root / "binding.json").write_text(
                json.dumps({"task_id": "task-1"}),
                encoding="utf-8",
            )
            report_path = step.output_dir / "plan-review.json"
            artifact_digest = (
                "sha256:" + hashlib.sha256(b"{}\n").hexdigest()
            )

            def handler(
                argv: tuple[str, ...],
                timeout_ms: int,
            ) -> dict[str, object]:
                if argv[:2] == ("orchestration", "dispatch"):
                    return {
                        "dispatch": {"id": "ctx-1"},
                        "preamble": "TASK_ID=task-1",
                    }
                if argv[:2] == ("terminal", "send"):
                    return {"send": {"accepted": True}}
                if argv[:2] == ("orchestration", "check"):
                    if "--ack" in argv:
                        return {"messages": [], "count": 0}
                    return {
                        "deliveryId": "delivery-1",
                        "messages": [
                            {
                                "type": "worker_done",
                                "task_id": "foreign",
                                "dispatch_id": "ctx-foreign",
                                "payload": "{}",
                            },
                            {
                                "type": "worker_done",
                                "report_path": str(report_path),
                                "payload": json.dumps(
                                     {
                                        "schema_version": 1,
                                         "taskId": "task-1",
                                         "dispatchId": "ctx-1",
                                        "outcome": "succeeded",
                                         "artifactDigest": artifact_digest,
                                     }
                                ),
                            },
                        ]
                    }
                self.fail(f"unexpected call: {argv}")

            foreign: list[dict[str, object]] = []
            dispatched: list[str] = []
            client = FakeOrcaClient(handler)
            handle, completion = dispatch_and_wait(
                client,  # type: ignore[arg-type]
                prepared,
                step,
                LaunchProfile(
                    ("codex", "exec", "-C", str(root), "-"),
                    (),
                    DIGEST_A,
                ),
                coordinator_handle="term-coordinator",
                orca_executable="C:\\fake\\orca.exe",
                runner_path=Path.cwd() / "worker_runner.py",
                step_timeout_ms=10_000,
                artifact_filename="plan-review.json",
                commit_dispatched=lambda item: dispatched.append(
                    item.dispatch_id
                ),
                foreign_message=foreign.append,
            )
            self.assertEqual("ctx-1", handle.dispatch_id)
            self.assertEqual(
                CompletionKind.WORKER_DONE,
                completion.kind,
            )
            self.assertEqual(["ctx-1"], dispatched)
            self.assertEqual(1, len(foreign))
            self.assertTrue(
                any(
                    "--ack" in argv and "delivery-1" in argv
                    for argv, _ in client.calls
                )
            )
            payload = json.loads(completion.payload_json or "{}")
            self.assertEqual(1, payload["schema_version"])
            self.assertEqual("task-1", payload["taskId"])
            self.assertEqual(str(report_path), payload["reportPath"])
            self.assertNotIn("outcome", payload)


class WorkerRunnerExtractionTest(unittest.TestCase):
    def test_extracts_claude_and_codex_artifacts(self) -> None:
        artifact = {"schema_version": 1, "value": "ok"}
        claude = json.dumps(
            {
                "result": (
                    "```json\n"
                    + json.dumps(artifact)
                    + "\n```"
                )
            }
        )
        codex = "\n".join(
            (
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": json.dumps(artifact),
                        },
                    }
                ),
            )
        )
        self.assertEqual(
            artifact,
            json.loads(extract_agent_artifact(claude)),
        )
        self.assertEqual(
            artifact,
            json.loads(extract_agent_artifact(codex)),
        )

    def test_extracts_largest_root_object_from_prefaced_output(self) -> None:
        artifact = {
            "schema_version": 1,
            "acceptance_criteria": [
                {
                    "criterion_id": "AC-1",
                    "verification_method": "Run the targeted test.",
                }
            ],
        }
        claude = json.dumps(
            {
                "result": (
                    "Revised plan follows.\n"
                    + json.dumps(artifact)
                )
            }
        )

        self.assertEqual(
            artifact,
            json.loads(extract_agent_artifact(claude)),
        )

    def test_runner_owns_prompt_artifact_and_worker_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            contract_path = root / "contract.md"
            output_path = root / "artifact.json"
            contract_path.write_text("contract only", encoding="utf-8")
            process = SimpleNamespace(returncode=0)
            process.communicate = lambda input, timeout: (
                b'{"schema_version":1}\n',
                b"",
            )
            job = {
                "profile_command": ["codex", "exec", "-"],
                "agent_cwd": str(root),
                "contract_path": str(contract_path),
                "output_path": str(output_path),
                "task_id": "task-1",
                "dispatch_id": "ctx-1",
                "coordinator_handle": "term-coordinator",
                "worker_handle": "term-worker",
                "orca_executable": "orca",
                "timeout_ms": 10_000,
                "preamble": "agent must send worker_done",
            }
            captured_input: list[bytes] = []

            def communicate(*, input: bytes, timeout: float):
                captured_input.append(input)
                return b'{"schema_version":1}\n', b""

            process.communicate = communicate
            with patch("worker_runner.subprocess.Popen", return_value=process):
                with patch("worker_runner._send") as send:
                    result = run_job(job)

            self.assertEqual(1, len(captured_input))
            prompt = captured_input[0].decode("utf-8")
            self.assertTrue(prompt.startswith("contract only\n\n"))
            self.assertIn("- task_id: `task-1`", prompt)
            self.assertIn("- dispatch_id: `ctx-1`", prompt)
            self.assertIn(
                "the deterministic wrapper owns artifact persistence",
                prompt,
            )
            self.assertEqual("PASS", result["status"])
            self.assertEqual(
                '{"schema_version":1}\n',
                output_path.read_text(encoding="utf-8"),
            )
            send.assert_called_once()

    def test_runner_accepts_strict_artifact_written_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            contract_path = root / "contract.md"
            output_path = root / "artifact.json"
            contract_path.write_text("contract only", encoding="utf-8")
            process = SimpleNamespace(returncode=0)

            def communicate(*, input: bytes, timeout: float):
                output_path.write_text(
                    '{\n  "schema_version": 1\n}\n',
                    encoding="utf-8",
                )
                return b"artifact written to output path\n", b""

            process.communicate = communicate
            job = {
                "profile_command": ["claude", "-p"],
                "agent_cwd": str(root),
                "contract_path": str(contract_path),
                "output_path": str(output_path),
                "task_id": "task-1",
                "dispatch_id": "ctx-1",
                "coordinator_handle": "term-coordinator",
                "worker_handle": "term-worker",
                "orca_executable": "orca",
                "timeout_ms": 10_000,
                "preamble": "unused",
            }
            with patch("worker_runner.subprocess.Popen", return_value=process):
                with patch("worker_runner._send") as send:
                    result = run_job(job)

            self.assertEqual("PASS", result["status"])
            self.assertEqual(
                '{"schema_version":1}\n',
                output_path.read_text(encoding="utf-8"),
            )
            send.assert_called_once()

    def test_runner_stamps_current_artifact_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            contract_path = root / "contract.md"
            output_path = root / "artifact.json"
            contract_path.write_text("contract only", encoding="utf-8")
            process = SimpleNamespace(returncode=0)
            process.communicate = lambda input, timeout: (
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_id": "task-stale",
                        "dispatch_id": "ctx-stale",
                    }
                ).encode(),
                b"",
            )
            job = {
                "profile_command": ["codex", "exec", "-"],
                "agent_cwd": str(root),
                "contract_path": str(contract_path),
                "output_path": str(output_path),
                "task_id": "task-current",
                "dispatch_id": "ctx-current",
                "coordinator_handle": "term-coordinator",
                "worker_handle": "term-worker",
                "orca_executable": "orca",
                "timeout_ms": 10_000,
                "preamble": "unused",
            }
            with patch("worker_runner.subprocess.Popen", return_value=process):
                with patch("worker_runner._send"):
                    result = run_job(job)

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("PASS", result["status"])
            self.assertEqual("task-current", artifact["task_id"])
            self.assertEqual("ctx-current", artifact["dispatch_id"])

    def test_worker_done_send_uses_current_orca_contract(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"result":{}}',
            stderr="",
        )
        job = {
            "orca_executable": "orca",
            "coordinator_handle": "term-coordinator",
            "worker_handle": "term-worker",
            "task_id": "task-1",
            "dispatch_id": "ctx-1",
        }
        with patch("worker_runner.subprocess.run", return_value=completed) as run:
            _send(
                job,
                message_type="worker_done",
                subject="done",
                payload={"artifactDigest": DIGEST_A},
                report_path=Path("artifact.json"),
            )

        command = run.call_args.args[0]
        self.assertNotIn("--to", command)
        self.assertNotIn("--from", command)
        self.assertNotIn("--task-id", command)
        self.assertNotIn("--dispatch-id", command)
        self.assertNotIn("--outcome", command)
        self.assertNotIn("--report-path", command)
        payload = json.loads(command[command.index("--payload") + 1])
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("task-1", payload["taskId"])
        self.assertEqual("ctx-1", payload["dispatchId"])
        self.assertEqual("succeeded", payload["outcome"])
        self.assertEqual("artifact.json", payload["reportPath"])
        self.assertEqual(DIGEST_A, payload["artifactDigest"])


if __name__ == "__main__":
    unittest.main()
