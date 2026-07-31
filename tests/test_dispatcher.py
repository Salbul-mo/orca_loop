from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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
from worker_runner import extract_agent_artifact


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
                    return {
                        "messages": [
                            {
                                "type": "worker_done",
                                "task_id": "foreign",
                                "dispatch_id": "ctx-foreign",
                                "payload": {},
                            },
                            {
                                "type": "worker_done",
                                "task_id": "task-1",
                                "dispatch_id": "ctx-1",
                                "report_path": str(report_path),
                                "payload": {
                                    "artifactDigest": artifact_digest
                                },
                            },
                        ]
                    }
                self.fail(f"unexpected call: {argv}")

            foreign: list[dict[str, object]] = []
            dispatched: list[str] = []
            handle, completion = dispatch_and_wait(
                FakeOrcaClient(handler),  # type: ignore[arg-type]
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
            payload = json.loads(completion.payload_json or "{}")
            self.assertEqual("task-1", payload["taskId"])
            self.assertEqual(str(report_path), payload["reportPath"])


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


if __name__ == "__main__":
    unittest.main()
