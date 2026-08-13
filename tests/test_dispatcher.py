from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orca_loop.dispatcher import (
    DispatchProvenanceError,
    _control_dir,
    dispatch_and_wait,
    prepare_task,
    provision_workers,
)
from orca_loop.generation import find_receipt, is_promoted, mark_promoted
from orca_loop.models import (
    CompletionKind,
    InboxClassification,
    LaunchProfile,
    PreparedTask,
    RenderedContract,
    Role,
    StepStage,
    WorkerHandle,
    WorkerKey,
)
from orca_loop.workspace import create_run_workspace
from tests.fakes import (
    FakeOrcaClient,
    assert_settlement_handshake,
    assert_supported_argv,
)
from worker_runner import (
    WorkerRunnerError,
    main as worker_main,
    _load_job,
    _send,
    extract_agent_artifact,
    run_job,
)


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
                orchestration_run_id="run-orca-1",
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
                    # Orca allows no custom --payload on a worker_done, so the
                    # digest arrives first on an artifact-ready status message
                    # and the worker_done only settles the Dispatch.
                    return {
                        "deliveryId": "delivery-1",
                        "messages": [
                            {
                                # A malformed foreign status must be durably
                                # quarantined without preventing the later
                                # valid settlement in the same Delivery.
                                "id": "msg-malformed",
                                "type": "status",
                                "payload": "{not valid JSON",
                            },
                            {
                                "id": "msg-foreign",
                                "type": "worker_done",
                                "task_id": "foreign",
                                "dispatch_id": "ctx-foreign",
                                "payload": "{}",
                            },
                            {
                                "id": "msg-ready",
                                "type": "status",
                                "payload": json.dumps(
                                    {
                                        "schema_version": 1,
                                        "taskId": "task-1",
                                        "dispatchId": "ctx-1",
                                        "artifactDigest": artifact_digest,
                                    }
                                ),
                            },
                            {
                                "id": "msg-done",
                                "type": "worker_done",
                                "report_path": str(report_path),
                                "payload": json.dumps(
                                    {
                                        "taskId": "task-1",
                                        "dispatchId": "ctx-1",
                                        "outcome": "succeeded",
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
                orchestration_run_id="run-orca-1",
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
            self.assertEqual("delivery-1", completion.delivery_id)
            self.assertFalse(
                any("--ack" in argv for argv, _ in client.calls)
            )
            self.assertTrue((step.root / "inbox" / "delivery-1.json").is_file())
            payload = json.loads(completion.payload_json or "{}")
            self.assertEqual(1, payload["schema_version"])
            self.assertEqual("task-1", payload["taskId"])
            self.assertEqual(str(report_path), payload["reportPath"])
            self.assertNotIn("outcome", payload)

            # The job the dispatcher actually ships must satisfy the runner's
            # own schema check.  Asserting it here closes the gap that let a
            # dispatcher-only field break every real dispatch while the fake
            # client kept the suite green.
            sent = next(
                argv
                for argv, _ in client.calls
                if argv[:2] == ("terminal", "send")
            )
            text = sent[sent.index("--text") + 1]
            encoded = text.rsplit("--job-base64 ", 1)[1].strip().strip("'")
            decoded = _load_job(encoded)
            self.assertEqual("run-orca-1", decoded["orchestration_run_id"])
            self.assertEqual("task-1", decoded["task_id"])

            # Every delivered row is classified and durable in the run inbox,
            # including the foreign one this step does not act on.
            receipt = find_receipt(_control_dir(step), "delivery-1")
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual(4, len(receipt.messages))
            self.assertEqual(4, len(receipt.classifications))
            self.assertIn(
                InboxClassification.DEFERRED,
                receipt.classifications,
            )
            self.assertIn(
                InboxClassification.QUARANTINED,
                receipt.classifications,
            )
            self.assertTrue(is_promoted(_control_dir(step), "msg-done"))

    def test_replayed_delivery_is_not_promoted_twice(self) -> None:
        """A lost ACK replays the batch; the step must not re-settle on it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, step = create_run_workspace(
                root,
                "run-1",
                "step-2",
                resume=False,
            )
            (step.input_dir / "contract.md").write_text(
                "contract",
                encoding="utf-8",
            )
            (step.root / "binding.json").write_text(
                json.dumps({"task_id": "task-2"}),
                encoding="utf-8",
            )
            control = _control_dir(step)
            control.mkdir(parents=True, exist_ok=True)
            # The previous step promoted this message before its ACK was lost.
            mark_promoted(control, ("msg-stale",))

            prepared = PreparedTask(
                "step-2",
                "task-2",
                WorkerHandle(
                    WorkerKey.CODEX_REVIEW,
                    "term-worker",
                    "wt-1",
                    "tab-1",
                    "leaf-1",
                ),
                Role.PLAN_REVIEWER,
                DIGEST_A,
            )

            def handler(argv: tuple[str, ...], _: int) -> dict[str, object]:
                if argv[:2] == ("orchestration", "dispatch"):
                    return {"dispatch": {"id": "ctx-2"},
                            "preamble": "TASK_ID=task-2"}
                if argv[:2] == ("terminal", "send"):
                    return {"send": {"accepted": True}}
                if argv[:2] == ("orchestration", "check"):
                    if "--ack" in argv:
                        return {"messages": [], "count": 0}
                    return {
                        "deliveryId": "delivery-9",
                        "messages": [{
                            "id": "msg-stale",
                            "type": "worker_done",
                            "payload": json.dumps({
                                "taskId": "task-2",
                                "dispatchId": "ctx-2",
                                "outcome": "succeeded",
                            }),
                        }],
                    }
                self.fail(f"unexpected call: {argv}")

            client = FakeOrcaClient(handler)
            _, completion = dispatch_and_wait(
                client,  # type: ignore[arg-type]
                prepared,
                step,
                LaunchProfile(
                    ("codex", "exec", "-C", str(root), "-"),
                    (),
                    DIGEST_A,
                ),
                orchestration_run_id="run-orca-1",
                coordinator_handle="term-coordinator",
                orca_executable="C:\\fake\\orca.exe",
                runner_path=Path.cwd() / "worker_runner.py",
                step_timeout_ms=1_500,
                artifact_filename="plan-review.json",
                commit_dispatched=lambda item: None,
            )

            # The replay is recognised, so it never settles the step a second
            # time and never fails for a missing artifact-ready.
            self.assertEqual(CompletionKind.STEP_TIMEOUT, completion.kind)
            receipt = find_receipt(control, "delivery-9")
            assert receipt is not None
            self.assertEqual(
                (InboxClassification.DUPLICATE,),
                receipt.classifications,
            )
            # A recognised replay is acknowledged, so it stops replaying.
            self.assertTrue(
                any("--ack" in argv for argv, _ in client.calls)
            )


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
            assert_settlement_handshake(send)

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
            }
            with patch("worker_runner.subprocess.Popen", return_value=process):
                with patch("worker_runner._send") as send:
                    result = run_job(job)

            self.assertEqual("PASS", result["status"])
            self.assertEqual(
                '{"schema_version":1}\n',
                output_path.read_text(encoding="utf-8"),
            )
            assert_settlement_handshake(send)

    def test_runner_rejects_conflicting_artifact_provenance(self) -> None:
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
            }
            with patch("worker_runner.subprocess.Popen", return_value=process):
                with patch("worker_runner._send"):
                    with self.assertRaisesRegex(
                        WorkerRunnerError,
                        "provenance conflicts",
                    ):
                        run_job(job)

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
            "orchestration_run_id": "run-orca-1",
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
        # Orca rejects a worker_done whose settlement signal lives only in the
        # payload JSON, so the typed flags are part of the contract.
        assert_supported_argv(tuple(command[1:]))
        self.assertEqual(
            "succeeded",
            command[command.index("--outcome") + 1],
        )
        self.assertEqual("task-1", command[command.index("--task-id") + 1])
        self.assertEqual("ctx-1", command[command.index("--dispatch-id") + 1])
        self.assertEqual(
            "artifact.json",
            command[command.index("--report-path") + 1],
        )
        self.assertEqual("run-orca-1", command[command.index("--run") + 1])
        # Orca refuses --payload alongside the structured flags, so a
        # worker_done must carry no JSON payload at all.
        self.assertNotIn("--payload", command)

    def test_status_carries_the_payload_worker_done_cannot(self) -> None:
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
            "orchestration_run_id": "run-orca-1",
        }
        with patch("worker_runner.subprocess.run", return_value=completed) as run:
            _send(
                job,
                message_type="status",
                subject="worker artifact ready",
                payload={"artifactDigest": DIGEST_A},
                report_path=Path("artifact.json"),
            )

        command = run.call_args.args[0]
        assert_supported_argv(tuple(command[1:]))
        self.assertNotIn("--outcome", command)
        self.assertNotIn("--task-id", command)
        payload = json.loads(command[command.index("--payload") + 1])
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("task-1", payload["taskId"])
        self.assertEqual("ctx-1", payload["dispatchId"])
        self.assertEqual("artifact.json", payload["reportPath"])
        self.assertEqual(DIGEST_A, payload["artifactDigest"])

    def test_failed_worker_done_sends_typed_failed_outcome(self) -> None:
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
            "orchestration_run_id": "run-orca-1",
        }
        with patch("worker_runner.subprocess.run", return_value=completed) as run:
            _send(
                job,
                message_type="worker_done",
                subject="worker runner failed",
                payload={"outcome": "failed", "reason": "denied"},
                report_path=None,
            )

        command = run.call_args.args[0]
        assert_supported_argv(tuple(command[1:]))
        self.assertEqual("failed", command[command.index("--outcome") + 1])

    def test_failure_sends_escalation_then_failed_settlement(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"result":{}}',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            job = {
                "profile_command": ["codex", "exec", "-"],
                "agent_cwd": str(root),
                "contract_path": str(root / "missing.md"),
                "output_path": str(root / "artifact.json"),
                "task_id": "task-1",
                "dispatch_id": "ctx-1",
                "coordinator_handle": "term-coordinator",
                "worker_handle": "term-worker",
                "orca_executable": "orca",
                "timeout_ms": 10_000,
                "log_dir": str(root / "logs"),
                "step_id": "g0001-plan",
                "orchestration_run_id": "run-orca-1",
            }
            encoded = base64.urlsafe_b64encode(
                json.dumps(job).encode("utf-8")
            ).decode("ascii")
            with patch("worker_runner.subprocess.run", return_value=completed):
                with patch("worker_runner._send") as send:
                    # main reports the blocked result on stderr by design.
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(
                            2,
                            worker_main(["--job-base64", encoded]),
                        )

        types = [c.kwargs["message_type"] for c in send.call_args_list]
        # The escalation carries the reason; the worker_done settles the
        # Dispatch as failed because a worker_done cannot carry either.
        self.assertEqual(["escalation", "worker_done"], types)
        self.assertIn("reason", send.call_args_list[0].kwargs["payload"])
        self.assertEqual(
            "failed",
            send.call_args_list[1].kwargs["payload"]["outcome"],
        )

    def test_worker_done_without_artifact_ready_is_quarantined(self) -> None:
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
            (step.root / "binding.json").write_text(
                json.dumps({"task_id": "task-1"}),
                encoding="utf-8",
            )
            prepared = PreparedTask(
                "step-1",
                "task-1",
                WorkerHandle(
                    WorkerKey.CODEX_REVIEW,
                    "term-worker",
                    "wt-1",
                    "tab-1",
                    "leaf-1",
                ),
                Role.PLAN_REVIEWER,
                DIGEST_A,
            )

            def handler(argv: tuple[str, ...], _: int) -> dict[str, object]:
                if argv[:2] == ("orchestration", "dispatch"):
                    return {"dispatch": {"id": "ctx-1"},
                            "preamble": "TASK_ID=task-1"}
                if argv[:2] == ("terminal", "send"):
                    return {"send": {"accepted": True}}
                if argv[:2] == ("orchestration", "check"):
                    if "--ack" in argv:
                        return {"messages": [], "count": 0}
                    return {
                        "deliveryId": "delivery-1",
                        "messages": [{
                            "id": "msg-done",
                            "type": "worker_done",
                            "payload": json.dumps({
                                "taskId": "task-1",
                                "dispatchId": "ctx-1",
                                "outcome": "succeeded",
                            }),
                        }],
                    }
                self.fail(f"unexpected call: {argv}")

            client = FakeOrcaClient(handler)
            with self.assertRaisesRegex(
                DispatchProvenanceError,
                "without an artifact-ready",
            ):
                dispatch_and_wait(
                    client,  # type: ignore[arg-type]
                    prepared,
                    step,
                    LaunchProfile(
                        ("codex", "exec", "-C", str(root), "-"),
                        (),
                        DIGEST_A,
                    ),
                    orchestration_run_id="run-orca-1",
                    coordinator_handle="term-coordinator",
                    orca_executable="C:\\fake\\orca.exe",
                    runner_path=Path.cwd() / "worker_runner.py",
                    step_timeout_ms=10_000,
                    artifact_filename="plan-review.json",
                    commit_dispatched=lambda item: None,
                )
            # Failing closed must not leave the delivery replaying forever.
            self.assertTrue(
                any("--ack" in argv for argv, _ in client.calls)
            )
            receipt = json.loads(
                (step.root / "inbox" / "delivery-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("artifact-ready", receipt["quarantine"])

    def test_send_rejects_an_outcome_orca_would_not_accept(self) -> None:
        job = {
            "orca_executable": "orca",
            "coordinator_handle": "term-coordinator",
            "worker_handle": "term-worker",
            "task_id": "task-1",
            "dispatch_id": "ctx-1",
            "orchestration_run_id": "run-orca-1",
        }
        with patch("worker_runner.subprocess.run") as run:
            with self.assertRaisesRegex(
                WorkerRunnerError,
                "outcome must be succeeded or failed",
            ):
                _send(
                    job,
                    message_type="worker_done",
                    subject="done",
                    payload={"outcome": "cancelled"},
                    report_path=None,
                )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
