from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orca_loop.dispatcher import _runner_command
from orca_loop.local_runtime import LocalCoordinatorClient
from orca_loop.orca_client import OrcaProtocolError
from worker_runner import _send


class LocalRuntimeTest(unittest.TestCase):
    def test_preflight_queries_are_read_only_until_runtime_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="local-test",
                coordinator_handle="local-coordinator",
            )
            status = json.loads(client.call(("status",), timeout_ms=1000).result_json)
            self.assertEqual(status["transport"], "local")
            shown = json.loads(
                client.call(
                    ("terminal", "show", "--terminal", "local-coordinator"),
                    timeout_ms=1000,
                ).result_json
            )
            self.assertEqual(
                shown["terminal"]["handle"],
                "local-coordinator",
            )
            self.assertFalse(client.state_path.exists())

    def test_local_runtime_mints_distinct_worker_and_dispatch_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="local-test",
                coordinator_handle="local-coordinator",
            )
            first = json.loads(
                client.call(
                    (
                        "terminal",
                        "create",
                        "--worktree",
                        "fixture",
                        "--title",
                        "worker one",
                    ),
                    timeout_ms=1000,
                ).result_json
            )["terminal"]
            second = json.loads(
                client.call(
                    (
                        "terminal",
                        "create",
                        "--worktree",
                        "fixture",
                        "--title",
                        "worker two",
                    ),
                    timeout_ms=1000,
                ).result_json
            )["terminal"]
            self.assertNotEqual(first["handle"], second["handle"])
            task = json.loads(
                client.call(
                    (
                        "orchestration",
                        "task-create",
                        "--task-title",
                        "fixture",
                        "--display-name",
                        "fixture",
                        "--spec",
                        "fixture",
                    ),
                    timeout_ms=1000,
                ).result_json
            )["task"]["id"]
            dispatched = json.loads(
                client.call(
                    (
                        "orchestration",
                        "dispatch",
                        "--task",
                        task,
                        "--to",
                        first["handle"],
                        "--from",
                        "local-coordinator",
                        "--return-preamble",
                    ),
                    timeout_ms=1000,
                ).result_json
            )
            dispatch_id = dispatched["dispatch"]["id"]
            self.assertIn(f"task_id={task}", dispatched["preamble"])
            self.assertIn(
                f"dispatch_id={dispatch_id}",
                dispatched["preamble"],
            )

    def test_worker_completion_queue_round_trips_without_orca_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="local-test",
                coordinator_handle="local-coordinator",
            )
            queue = client.completion_dir
            report = root / "artifact.json"
            report.write_text("{}\n", encoding="utf-8")
            job = {
                "task_id": "task-1",
                "dispatch_id": "dispatch-1",
                "coordinator_handle": "local-coordinator",
                "worker_handle": "worker-1",
                "orca_executable": "<local-runtime>",
                "completion_dir": str(queue),
            }
            _send(
                job,
                message_type="worker_done",
                subject="done",
                payload={"artifactDigest": "sha256:test"},
                report_path=report,
            )
            checked = json.loads(
                client.call(
                    (
                        "orchestration",
                        "check",
                        "--terminal",
                        "local-coordinator",
                        "--types",
                        "worker_done,escalation",
                        "--wait",
                        "--timeout-ms",
                        "1000",
                    ),
                    timeout_ms=1500,
                ).result_json
            )
            self.assertEqual(len(checked["messages"]), 1)
            message = checked["messages"][0]["message"]
            self.assertEqual(message["type"], "worker_done")
            self.assertEqual(message["taskId"], "task-1")
            self.assertEqual(message["dispatchId"], "dispatch-1")
            self.assertEqual(message["reportPath"], str(report))


    def test_gate_create_emits_durable_human_supervision_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="supervision-test",
                coordinator_handle="local-coordinator",
            )
            publisher_patch = mock.patch.object(
                client,
                "_publish_supervision",
                return_value=True,
            )
            publisher = publisher_patch.start()
            self.addCleanup(publisher_patch.stop)
            task = json.loads(
                client.call(
                    (
                        "orchestration", "task-create",
                        "--task-title", "decision",
                        "--display-name", "decision",
                        "--spec", "human decision",
                    ),
                    timeout_ms=1000,
                ).result_json
            )["task"]["id"]
            created = json.loads(
                client.call(
                    (
                        "orchestration", "gate-create",
                        "--task", task,
                        "--question", "Choose the product behavior.",
                        "--options", '["revise_design","reject"]',
                    ),
                    timeout_ms=1000,
                ).result_json
            )
            gate_id = created["id"]
            notification_path = client.notification_dir / f"{gate_id}.json"
            self.assertTrue(notification_path.is_file())
            notification = json.loads(notification_path.read_text(encoding="utf-8"))
            self.assertEqual(notification["type"], "human_supervision_required")
            self.assertEqual(notification["status"], "pending")
            self.assertEqual(notification["gateId"], gate_id)
            self.assertEqual(created["notification"], notification)

            resolution = {
                "decision": "reject",
                "decision_note": "human chose to stop",
                "affected_acceptance_criteria": [],
                "affected_finding_ids": [],
                "report_digest": "sha256:" + "a" * 64,
            }
            resolved = client.resolve_gate(
                gate_id=gate_id,
                resolution=resolution,
            )
            self.assertEqual(resolved["status"], "resolved")
            self.assertEqual(resolved["resolution"], resolution)
            notification = json.loads(
                notification_path.read_text(encoding="utf-8")
            )
            self.assertEqual(notification["status"], "resolved")
            listed = json.loads(
                client.call(
                    (
                        "orchestration", "gate-list",
                        "--task", task,
                        "--status", "resolved",
                    ),
                    timeout_ms=1000,
                ).result_json
            )
            self.assertEqual([gate_id], [item["id"] for item in listed["gates"]])
            self.assertEqual(2, publisher.call_count)
            self.assertEqual(
                "pending",
                publisher.call_args_list[0].args[0]["status"],
            )
            self.assertEqual(
                "resolved",
                publisher.call_args_list[1].args[0]["status"],
            )

    def test_gate_rejects_stale_resolution_without_consuming_pending_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="stale-decision-test",
                coordinator_handle="local-coordinator",
            )
            with mock.patch.object(client, "_publish_supervision", return_value=True):
                task = json.loads(
                    client.call(
                        (
                            "orchestration", "task-create",
                            "--task-title", "decision",
                            "--display-name", "decision",
                            "--spec", "human decision",
                        ),
                        timeout_ms=1000,
                    ).result_json
                )["task"]["id"]
                expected_digest = "sha256:" + "a" * 64
                created = json.loads(
                    client.call(
                        (
                            "orchestration", "gate-create",
                            "--task", task,
                            "--question", "Choose.",
                            "--options", '["reject"]',
                            "--report-digest", expected_digest,
                        ),
                        timeout_ms=1000,
                    ).result_json
                )
                gate_id = created["id"]
                stale = {
                    "decision": "reject",
                    "decision_note": "stale",
                    "affected_acceptance_criteria": [],
                    "affected_finding_ids": [],
                    "report_digest": "sha256:" + "b" * 64,
                }
                with self.assertRaisesRegex(
                    OrcaProtocolError,
                    "stale human decision report digest",
                ):
                    client.resolve_gate(
                        gate_id=gate_id,
                        resolution=stale,
                        expected_report_digest=expected_digest,
                    )
                gate_path = client.gate_dir / f"{gate_id}.json"
                pending = json.loads(gate_path.read_text(encoding="utf-8"))
                self.assertEqual("pending", pending["status"])

                valid = dict(stale)
                valid["report_digest"] = expected_digest
                resolved = client.resolve_gate(
                    gate_id=gate_id,
                    resolution=valid,
                    expected_report_digest=expected_digest,
                )
                self.assertEqual("resolved", resolved["status"])

    def test_gate_list_retries_unacknowledged_supervision_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="publish-retry-test",
                coordinator_handle="local-coordinator",
            )
            with mock.patch.object(
                client,
                "_publish_supervision",
                side_effect=[False, True],
            ) as publisher:
                task = json.loads(
                    client.call(
                        (
                            "orchestration", "task-create",
                            "--task-title", "decision",
                            "--display-name", "decision",
                            "--spec", "human decision",
                        ),
                        timeout_ms=1000,
                    ).result_json
                )["task"]["id"]
                client.call(
                    (
                        "orchestration", "gate-create",
                        "--task", task,
                        "--question", "Choose.",
                        "--options", '["reject"]',
                    ),
                    timeout_ms=1000,
                )
                self.assertEqual(1, publisher.call_count)
                client.call(
                    ("orchestration", "gate-list", "--task", task),
                    timeout_ms=1000,
                )
                self.assertEqual(2, publisher.call_count)
                client.call(
                    ("orchestration", "gate-list", "--task", task),
                    timeout_ms=1000,
                )
                self.assertEqual(2, publisher.call_count)

    def test_resolved_supervision_publish_is_retried_until_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="resolved-retry-test",
                coordinator_handle="local-coordinator",
            )
            with mock.patch.object(
                client,
                "_publish_supervision",
                side_effect=[True, False, True],
            ) as publisher:
                task = json.loads(
                    client.call(
                        (
                            "orchestration", "task-create",
                            "--task-title", "decision",
                            "--display-name", "decision",
                            "--spec", "human decision",
                        ),
                        timeout_ms=1000,
                    ).result_json
                )["task"]["id"]
                created = json.loads(
                    client.call(
                        (
                            "orchestration", "gate-create",
                            "--task", task,
                            "--question", "Choose.",
                            "--options", '["reject"]',
                        ),
                        timeout_ms=1000,
                    ).result_json
                )
                resolution = {
                    "decision": "reject",
                    "decision_note": "stop",
                    "affected_acceptance_criteria": [],
                    "affected_finding_ids": [],
                    "report_digest": "sha256:" + "c" * 64,
                }
                client.resolve_gate(
                    gate_id=created["id"],
                    resolution=resolution,
                )
                self.assertEqual(2, publisher.call_count)
                client.call(
                    ("orchestration", "gate-list", "--task", task),
                    timeout_ms=1000,
                )
                self.assertEqual(3, publisher.call_count)
                self.assertEqual(
                    "resolved",
                    publisher.call_args_list[-1].args[0]["status"],
                )
                client.call(
                    ("orchestration", "gate-list", "--task", task),
                    timeout_ms=1000,
                )
                self.assertEqual(3, publisher.call_count)

    def test_local_terminal_launches_worker_runner_and_receives_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="local-worker-test",
                coordinator_handle="local-coordinator",
            )
            terminal = json.loads(
                client.call(
                    (
                        "terminal",
                        "create",
                        "--worktree",
                        "fixture",
                        "--title",
                        "worker",
                    ),
                    timeout_ms=1000,
                ).result_json
            )["terminal"]["handle"]
            provider = root / "provider.py"
            provider.write_text(
                "import sys\nsys.stdin.read()\nprint('{}')\n",
                encoding="utf-8",
            )
            contract = root / "contract.md"
            contract.write_text("Return JSON only.", encoding="utf-8")
            output = root / "artifact.json"
            job = {
                "profile_command": [sys.executable, str(provider)],
                "agent_cwd": str(root),
                "contract_path": str(contract),
                "output_path": str(output),
                "task_id": "task-process",
                "dispatch_id": "dispatch-process",
                "coordinator_handle": "local-coordinator",
                "worker_handle": terminal,
                "orca_executable": "<local-runtime>",
                "timeout_ms": 10_000,
                "preamble": (
                    "task_id=task-process\n"
                    "dispatch_id=dispatch-process"
                ),
                "completion_dir": str(client.completion_dir),
            }
            runner = (
                Path(__file__).resolve().parents[1] / "worker_runner.py"
            )
            command = _runner_command(job, runner)
            client.call(
                (
                    "terminal",
                    "send",
                    "--terminal",
                    terminal,
                    "--text",
                    command,
                    "--enter",
                ),
                timeout_ms=1000,
            )
            checked = json.loads(
                client.call(
                    (
                        "orchestration",
                        "check",
                        "--terminal",
                        "local-coordinator",
                        "--types",
                        "worker_done,escalation",
                        "--wait",
                        "--timeout-ms",
                        "10000",
                    ),
                    timeout_ms=11_000,
                ).result_json
            )
            self.assertEqual(len(checked["messages"]), 1)
            message = checked["messages"][0]["message"]
            self.assertEqual(message["type"], "worker_done")
            self.assertEqual(message["taskId"], "task-process")
            self.assertEqual(message["dispatchId"], "dispatch-process")
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
