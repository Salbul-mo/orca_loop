from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.config import (
    PreflightResult,
    empty_test_policy,
    PreflightError,
    parse_run_arguments,
    run_preflight,
)
from orca_loop.contracts import digest_value
from orca_loop.models import (
    LoopState,
    PermissionCheck,
    PermissionFeasibilityReport,
    PermissionStrategy,
    RunStatus,
    ValidationStatus,
)
from run_loop import (
    EXIT_READY,
    EXIT_REJECTED,
    EXIT_RUNTIME_FAILURE,
    EXIT_USER_REQUIRED,
    _initialize,
    exit_code,
)
from orca_loop.locking import (
    RunLockError,
    acquire_run_lock,
    release_run_lock,
)
from tests.fakes import FakeOrcaClient


class CliConfigurationTest(unittest.TestCase):
    def test_parser_keeps_approved_five_round_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            report = root / "permission.json"
            request.write_text("request", encoding="utf-8")
            report.write_text("{}", encoding="utf-8")
            arguments = parse_run_arguments(
                (
                    "--run-id",
                    "run-1",
                    "--request",
                    str(request),
                    "--worktree",
                    str(root),
                    "--coordinator-handle",
                    "term-1",
                    "--permission-report",
                    str(report),
                    "--dry-run",
                ),
                harness_root=root,
            )
            self.assertEqual(
                5,
                arguments.config.plan_consensus_round_limit,
            )
            self.assertEqual(
                5,
                arguments.config.code_consensus_round_limit,
            )

    def test_preflight_rejects_dirty_worktree_before_orca_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(
                ("git", "init"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.name", "Test"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("base", encoding="utf-8")
            subprocess.run(
                ("git", "add", "tracked.txt"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "commit", "-m", "base"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            request = root / "request.md"
            permission = root / "permission.json"
            request.write_text("request", encoding="utf-8")
            permission.write_text("{}", encoding="utf-8")
            arguments = parse_run_arguments(
                (
                    "--run-id",
                    "run-1",
                    "--request",
                    str(request),
                    "--worktree",
                    str(root),
                    "--coordinator-handle",
                    "term-1",
                    "--permission-report",
                    str(permission),
                ),
                harness_root=root,
            )
            client = FakeOrcaClient(
                lambda _argv, _timeout: self.fail(
                    "Orca must not be called for dirty worktree"
                )
            )
            with self.assertRaisesRegex(
                (PreflightError, ValueError),
                "permission|clean",
            ):
                run_preflight(arguments, client)  # type: ignore[arg-type]

    def test_preflight_accepts_live_status_shape_and_strategy_d(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(
                ("git", "init"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.name", "Test"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            request = root / "request.md"
            permission = root / "permission.json"
            request.write_text("request", encoding="utf-8")
            report_value = {
                "schema_version": 1,
                "run_id": "spike",
                "status": "PASS",
                "strategy": "D",
                "checks": [
                    {
                        "check_id": f"V-PERM-0{index}",
                        "status": "PASS",
                        "evidence": ["live evidence"],
                    }
                    for index in range(1, 6)
                ],
                "evidence": ["live evidence"],
                "orca_version": "1.4.159",
                "canonical_path": str(permission),
            }
            report_value["report_digest"] = digest_value(report_value)
            permission.write_text(
                json.dumps(report_value),
                encoding="utf-8",
            )
            subprocess.run(
                ("git", "add", "request.md", "permission.json"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "commit", "-m", "fixture"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            arguments = parse_run_arguments(
                (
                    "--run-id",
                    "run-1",
                    "--request",
                    str(request),
                    "--worktree",
                    str(root),
                    "--coordinator-handle",
                    "term-1",
                    "--permission-report",
                    str(permission),
                ),
                harness_root=root,
            )

            def handler(
                argv: tuple[str, ...],
                _: int,
            ) -> dict[str, object]:
                if argv == ("status",):
                    return {
                        "runtime": {
                            "state": "ready",
                            "reachable": True,
                            "appVersion": "1.4.159",
                        },
                        "graph": {"state": "ready"},
                    }
                if argv[:2] == ("terminal", "show"):
                    return {"terminal": {"handle": "term-1"}}
                self.fail(f"unexpected Orca call: {argv}")

            result = run_preflight(
                arguments,
                FakeOrcaClient(handler),  # type: ignore[arg-type]
            )
            self.assertEqual("1.4.159", result.orca_version)


class RunLockTest(unittest.TestCase):
    def test_second_lock_is_rejected_and_owner_can_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lock = acquire_run_lock(root, root, "run-1")
            with self.assertRaises(RunLockError):
                acquire_run_lock(root, root, "run-2")
            release_run_lock(lock)
            self.assertFalse(lock.path.exists())


class RunEntryPointTest(unittest.TestCase):
    def test_initialize_records_four_workers_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            permission_path = root / "permission.json"
            request.write_text("request", encoding="utf-8")
            permission_path.write_text("{}", encoding="utf-8")
            subprocess.run(
                ("git", "init"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.name", "Test"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "add", "request.md", "permission.json"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ("git", "commit", "-m", "fixture"),
                cwd=root,
                capture_output=True,
                check=True,
            )
            arguments = parse_run_arguments(
                (
                    "--run-id",
                    "run-1",
                    "--request",
                    str(request),
                    "--worktree",
                    str(root),
                    "--coordinator-handle",
                    "term-coordinator",
                    "--permission-report",
                    str(permission_path),
                ),
                harness_root=root,
            )
            report = PermissionFeasibilityReport(
                schema_version=1,
                run_id="spike",
                status=ValidationStatus.PASS,
                strategy=PermissionStrategy.READONLY_REPOSITORY,
                checks=(
                    PermissionCheck(
                        "V-PERM-01",
                        ValidationStatus.PASS,
                        ("evidence",),
                    ),
                ),
                evidence=("evidence",),
                orca_version="1.4.159",
                canonical_path=str(permission_path),
                report_digest="sha256:" + "a" * 64,
            )
            preflight = PreflightResult(
                arguments,
                empty_test_policy(),
                report,
                "1.4.159",
                "a" * 40,
            )
            counter = 0

            def handler(
                argv: tuple[str, ...],
                _: int,
            ) -> dict[str, object]:
                nonlocal counter
                if argv[:2] == ("terminal", "create"):
                    counter += 1
                    return {
                        "terminal": {
                            "handle": f"term-{counter}",
                            "tabId": f"tab-{counter}",
                            "leafId": f"leaf-{counter}",
                            "worktreeId": "worktree-1",
                        }
                    }
                if argv[:2] == ("terminal", "show"):
                    return {"terminal": {"status": "running"}}
                self.fail(f"unexpected Orca call: {argv}")

            controller, pool = _initialize(
                preflight,
                FakeOrcaClient(handler),  # type: ignore[arg-type]
            )
            self.assertEqual(LoopState.PLAN, controller.state.state)
            self.assertEqual(4, len(pool.workers))
            self.assertEqual(pool.workers, controller.state.worker_handles)

    def test_exit_code_mapping_is_exact(self) -> None:
        from tests.test_coordinator import initial_state
        from dataclasses import replace

        state = initial_state()
        self.assertEqual(
            EXIT_READY,
            exit_code(
                replace(
                    state,
                    state=LoopState.READY_FOR_MERGE,
                    status=RunStatus.READY,
                )
            ),
        )
        self.assertEqual(
            EXIT_REJECTED,
            exit_code(
                replace(
                    state,
                    state=LoopState.REJECTED,
                    status=RunStatus.REJECTED,
                )
            ),
        )
        self.assertEqual(
            EXIT_USER_REQUIRED,
            exit_code(
                replace(
                    state,
                    state=LoopState.USER_DECISION_REQUIRED,
                    status=RunStatus.BLOCKED,
                )
            ),
        )
        self.assertEqual(
            EXIT_RUNTIME_FAILURE,
            exit_code(
                replace(
                    state,
                    state=LoopState.FAILED,
                    status=RunStatus.FAILED,
                )
            ),
        )
