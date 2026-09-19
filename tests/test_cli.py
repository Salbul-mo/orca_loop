from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from orca_loop.config import (
    ConfigurationError,
    PreflightResult,
    default_agent_runtime_config,
    empty_test_policy,
    load_master_runtime_snapshot,
    persist_agent_runtime_snapshot,
    persist_master_runtime_snapshot,
    persist_agent_runtime_source,
    PreflightError,
    parse_run_arguments,
    prepare_agent_runtime,
    prepare_master_runtime,
    resolve_agent_runtime,
    run_preflight,
)
from orca_loop.contracts import (
    build_agent_runtime_config,
    build_master_runtime_config,
    default_agent_provider,
    digest_value,
    parse_agent_runtime_config,
    serialize_agent_runtime_config,
    serialize_master_runtime_config,
    serialize_json,
)
from orca_loop.models import (
    AgentProvider,
    AgentRuntimeOptions,
    AffectedFile,
    AffectedFileOperation,
    ArtifactKind,
    CodeReviewVerdict,
    DecisionValue,
    EscalationCode,
    EscalationTrigger,
    InformationalFinding,
    LoopState,
    MasterAction,
    MasterDecision,
    MasterRuntimeOptions,
    PermissionProfile,
    PlanReviewVerdict,
    ReviewArtifact,
    Role,
    RunStatus,
    Side,
    SignalKind,
    StepExecutionResult,
    TestGateStatus as GateStatus,
    TransitionSignal,
    WorkerKey,
)
from orca_loop.coordinator import OrcaLoopError, commit_step_transition
from run_loop import (
    EXIT_READY,
    EXIT_REJECTED,
    EXIT_RUNTIME_FAILURE,
    EXIT_USER_REQUIRED,
    _initialize,
    _route_final_decision,
    _route_test_result,
    _route_worker_completion,
    _validate_final_master_decision,
    _validate_test_result_master_decision,
    _validate_worker_completion_master_decision,
    _resume,
    exit_code,
)
from orca_loop.locking import (
    RunLockError,
    acquire_run_lock,
    release_run_lock,
)
from tests.fakes import FakeOrcaClient
from tests.test_coordinator import plan as coordinator_plan
from tests.test_ledger import decision as ledger_decision, finding as ledger_finding


class CliConfigurationTest(unittest.TestCase):
    def arguments(
        self,
        root: Path,
        *extra: str,
        resume: bool = False,
    ):
        request = root / "request.md"
        request.write_text("request", encoding="utf-8")
        values = [
            "--run-id",
            "run-1",
            "--request",
            str(request),
            "--worktree",
            str(root),
            "--coordinator-handle",
            "term-1",
            *extra,
        ]
        if resume:
            values.append("--resume")
        return parse_run_arguments(tuple(values), harness_root=root)

    def test_parser_keeps_approved_five_round_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            request.write_text("request", encoding="utf-8")
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

    def test_agent_cli_overrides_are_typed_and_duplicates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(
                root,
                "--agent-provider",
                "claude_planner=codex",
                "--agent-model",
                "codex_implementer=gpt-test",
                "--agent-effort",
                "codex_implementer=inherit",
            )
            request = arguments.agent_runtime_request
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(
                ((WorkerKey.CLAUDE_PLANNER, AgentProvider.CODEX),),
                request.provider_overrides,
            )
            self.assertEqual(
                ((WorkerKey.CODEX_IMPLEMENTER, "gpt-test"),),
                request.model_overrides,
            )
            self.assertEqual(
                ((WorkerKey.CODEX_IMPLEMENTER, None),),
                request.effort_overrides,
            )
            with self.assertRaisesRegex(ConfigurationError, "duplicates"):
                self.arguments(
                    root,
                    "--agent-model",
                    "codex_review=one",
                    "--agent-model",
                    "codex_review=two",
                )
            with self.assertRaisesRegex(ConfigurationError, "unknown worker"):
                self.arguments(
                    root,
                    "--agent-effort",
                    "unknown=high",
                )
            with self.assertRaisesRegex(ConfigurationError, "claude or codex"):
                self.arguments(
                    root,
                    "--agent-provider",
                    "codex_review=unknown",
                )

    def test_master_config_is_parsed_independently_from_worker_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root / "master-runtime-source.json"
            expected = build_master_runtime_config(
                MasterRuntimeOptions(
                    AgentProvider.CLAUDE,
                    "master-model",
                    "high",
                )
            )
            config_path.write_text(
                serialize_master_runtime_config(expected) + "\n",
                encoding="utf-8",
            )

            arguments = self.arguments(
                root,
                "--master-config",
                str(config_path),
            )
            self.assertIsNotNone(arguments.master_runtime_request)
            assert arguments.master_runtime_request is not None
            self.assertEqual(expected, arguments.master_runtime_request.config)
            self.assertIsNotNone(arguments.agent_runtime_request)

            prepared = prepare_master_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    "1.4.159",
                    "a" * 40,
                )
            )
            self.assertEqual(expected, prepared.master_runtime)

    def test_master_runtime_resume_uses_snapshot_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "runs" / "run-1" / "control"
            control.mkdir(parents=True)
            stored = build_master_runtime_config(
                MasterRuntimeOptions(
                    AgentProvider.CLAUDE,
                    "stored-master-model",
                    "high",
                )
            )
            persist_master_runtime_snapshot(
                control,
                "run-1",
                stored,
                None,
            )

            arguments = self.arguments(root, resume=True)
            prepared = prepare_master_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    "1.4.159",
                    "a" * 40,
                )
            )
            self.assertEqual(stored, prepared.master_runtime)

            config_path = root / "different-master-runtime.json"
            different = build_master_runtime_config(
                MasterRuntimeOptions(
                    AgentProvider.CODEX,
                    "different-master-model",
                    None,
                )
            )
            config_path.write_text(
                serialize_master_runtime_config(different) + "\n",
                encoding="utf-8",
            )
            drifted = self.arguments(
                root,
                "--master-config",
                str(config_path),
                resume=True,
            )
            with self.assertRaisesRegex(PreflightError, "master runtime.*drift"):
                prepare_master_runtime(
                    PreflightResult(
                        drifted,
                        empty_test_policy(),
                        "1.4.159",
                        "a" * 40,
                    )
                )

    def test_wizard_persists_base_but_not_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root.parent / f"{root.name}-agent-runtime.json"
            self.addCleanup(config_path.unlink, missing_ok=True)
            initial = build_agent_runtime_config(
                tuple(
                    AgentRuntimeOptions(
                        worker,
                        default_agent_provider(worker),
                        f"model-{worker.value}",
                        "high",
                    )
                    for worker in WorkerKey
                )
            )
            config_path.write_text(
                serialize_agent_runtime_config(initial) + "\n",
                encoding="utf-8",
            )
            arguments = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--configure-agents",
                "--agent-model",
                "codex_implementer=one-shot-model",
            )
            answers = iter(
                ["", "inherit", "", *("",) * 9, "y"]
            )
            stderr = io.StringIO()
            resolution = resolve_agent_runtime(
                arguments.agent_runtime_request,
                resume=False,
                worktree_path=root,
                interactive=True,
                input_fn=lambda: next(answers),
                stderr=stderr,
            )
            persist_agent_runtime_source(resolution)
            persisted = parse_agent_runtime_config(
                config_path.read_text(encoding="utf-8")
            )
            persisted_by_worker = {
                item.worker_key: item for item in persisted.agents
            }
            resolved_by_worker = {
                item.worker_key: item for item in resolution.config.agents
            }
            self.assertIsNone(
                persisted_by_worker[WorkerKey.CLAUDE_PLANNER].model
            )
            self.assertNotEqual(
                "one-shot-model",
                persisted_by_worker[WorkerKey.CODEX_IMPLEMENTER].model,
            )
            self.assertEqual(
                "one-shot-model",
                resolved_by_worker[WorkerKey.CODEX_IMPLEMENTER].model,
            )
            self.assertIn("Resolved agent runtime:", stderr.getvalue())

    def test_provider_change_rejects_stale_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root.parent / f"{root.name}-provider-runtime.json"
            self.addCleanup(config_path.unlink, missing_ok=True)
            initial = build_agent_runtime_config(
                tuple(
                    AgentRuntimeOptions(
                        worker,
                        default_agent_provider(worker),
                        "provider-specific-model",
                        "high",
                    )
                    for worker in WorkerKey
                )
            )
            config_path.write_text(
                serialize_agent_runtime_config(initial),
                encoding="utf-8",
            )
            stale = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--agent-provider",
                "claude_planner=codex",
            )
            with self.assertRaisesRegex(ConfigurationError, "provider-specific"):
                resolve_agent_runtime(
                    stale.agent_runtime_request,
                    resume=False,
                    worktree_path=root,
                    interactive=False,
                )

            explicit = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--agent-provider",
                "claude_planner=codex",
                "--agent-model",
                "claude_planner=inherit",
                "--agent-effort",
                "claude_planner=inherit",
            )
            resolved = resolve_agent_runtime(
                explicit.agent_runtime_request,
                resume=False,
                worktree_path=root,
                interactive=False,
            ).config
            planner = {
                item.worker_key: item for item in resolved.agents
            }[WorkerKey.CLAUDE_PLANNER]
            self.assertEqual(AgentProvider.CODEX, planner.provider)
            self.assertIsNone(planner.model)
            self.assertIsNone(planner.effort)

    def test_implementer_provider_override_needs_no_permission_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(
                root,
                "--agent-provider",
                "codex_implementer=claude",
            )
            base = PreflightResult(
                arguments,
                empty_test_policy(),
                "1.4.159",
                "a" * 40,
            )
            prepared = prepare_agent_runtime(
                base,
                interactive=False,
                stderr=io.StringIO(),
            )
            assert prepared.agent_runtime is not None
            implementer = {
                item.worker_key: item
                for item in prepared.agent_runtime.agents
            }[WorkerKey.CODEX_IMPLEMENTER]
            self.assertEqual(AgentProvider.CLAUDE, implementer.provider)

    def test_wizard_can_create_a_new_strict_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root.parent / f"{root.name}-new-agent-runtime.json"
            self.addCleanup(config_path.unlink, missing_ok=True)
            arguments = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--configure-agents",
            )
            answers = iter([*("",) * 12, "y"])
            resolution = resolve_agent_runtime(
                arguments.agent_runtime_request,
                resume=False,
                worktree_path=root,
                interactive=True,
                input_fn=lambda: next(answers),
                stderr=io.StringIO(),
            )
            persisted_path = persist_agent_runtime_source(resolution)
            self.assertEqual(config_path.resolve(), persisted_path)
            self.assertEqual(
                default_agent_runtime_config(),
                parse_agent_runtime_config(
                    config_path.read_text(encoding="utf-8")
                ),
            )

    def test_wizard_provider_change_clears_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root.parent / f"{root.name}-wizard-provider.json"
            self.addCleanup(config_path.unlink, missing_ok=True)
            initial = build_agent_runtime_config(
                tuple(
                    AgentRuntimeOptions(
                        worker,
                        default_agent_provider(worker),
                        "provider-specific-model",
                        "high",
                    )
                    for worker in WorkerKey
                )
            )
            config_path.write_text(
                serialize_agent_runtime_config(initial),
                encoding="utf-8",
            )
            arguments = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--configure-agents",
            )
            answers = iter(["codex", "", "", *("",) * 9, "y"])
            resolution = resolve_agent_runtime(
                arguments.agent_runtime_request,
                resume=False,
                worktree_path=root,
                interactive=True,
                input_fn=lambda: next(answers),
                stderr=io.StringIO(),
            )
            persist_agent_runtime_source(resolution)
            persisted = parse_agent_runtime_config(
                config_path.read_text(encoding="utf-8")
            )
            planner = {
                item.worker_key: item for item in persisted.agents
            }[WorkerKey.CLAUDE_PLANNER]
            self.assertEqual(AgentProvider.CODEX, planner.provider)
            self.assertIsNone(planner.model)
            self.assertIsNone(planner.effort)

    def test_wizard_cancel_and_concurrent_change_preserve_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root.parent / f"{root.name}-agent-runtime.json"
            self.addCleanup(config_path.unlink, missing_ok=True)
            original = serialize_agent_runtime_config(
                default_agent_runtime_config()
            ) + "\n"
            config_path.write_text(original, encoding="utf-8")
            arguments = self.arguments(
                root,
                "--agent-config",
                str(config_path),
                "--configure-agents",
            )
            with self.assertRaisesRegex(ConfigurationError, "interactive"):
                resolve_agent_runtime(
                    arguments.agent_runtime_request,
                    resume=False,
                    worktree_path=root,
                    interactive=False,
                    stderr=io.StringIO(),
                )
            with self.assertRaisesRegex(ConfigurationError, "cancelled"):
                resolve_agent_runtime(
                    arguments.agent_runtime_request,
                    resume=False,
                    worktree_path=root,
                    interactive=True,
                    input_fn=lambda: (_ for _ in ()).throw(EOFError()),
                    stderr=io.StringIO(),
                )
            cancelled = iter([*("",) * 12, "n"])
            with self.assertRaisesRegex(ConfigurationError, "cancelled"):
                resolve_agent_runtime(
                    arguments.agent_runtime_request,
                    resume=False,
                    worktree_path=root,
                    interactive=True,
                    input_fn=lambda: next(cancelled),
                    stderr=io.StringIO(),
                )
            self.assertEqual(original, config_path.read_text(encoding="utf-8"))

            accepted = iter([*("",) * 12, "y"])
            resolution = resolve_agent_runtime(
                arguments.agent_runtime_request,
                resume=False,
                worktree_path=root,
                interactive=True,
                input_fn=lambda: next(accepted),
                stderr=io.StringIO(),
            )
            config_path.write_text("external change\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "changed during"):
                persist_agent_runtime_source(resolution)

    def test_resume_uses_snapshot_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "runs" / "run-1" / "control"
            control.mkdir(parents=True)
            stored = build_agent_runtime_config(
                tuple(
                    AgentRuntimeOptions(
                        worker,
                        default_agent_provider(worker),
                        "stored-model",
                        "high",
                    )
                    for worker in WorkerKey
                )
            )
            persist_agent_runtime_snapshot(
                control,
                "run-1",
                stored,
                None,
            )
            arguments = self.arguments(root, resume=True)
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    "1.4.159",
                    "a" * 40,
                ),
                interactive=False,
                stderr=io.StringIO(),
            )
            self.assertEqual(stored, prepared.agent_runtime)

            drifted_arguments = self.arguments(
                root,
                "--agent-model",
                "codex_review=different-model",
                resume=True,
            )
            with self.assertRaisesRegex(PreflightError, "drift"):
                prepare_agent_runtime(
                    PreflightResult(
                        drifted_arguments,
                        empty_test_policy(),
                        "1.4.159",
                        "a" * 40,
                    ),
                    interactive=False,
                    stderr=io.StringIO(),
                )

            provider_drift = self.arguments(
                root,
                "--agent-provider",
                "claude_planner=codex",
                "--agent-model",
                "claude_planner=inherit",
                "--agent-effort",
                "claude_planner=inherit",
                resume=True,
            )
            with self.assertRaisesRegex(PreflightError, "drift"):
                prepare_agent_runtime(
                    PreflightResult(
                        provider_drift,
                        empty_test_policy(),
                        "1.4.159",
                        "a" * 40,
                    ),
                    interactive=False,
                    stderr=io.StringIO(),
                )

    def test_master_runtime_snapshot_roundtrip_and_immutability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "runs" / "run-1" / "control"
            control.mkdir(parents=True)
            stored = build_master_runtime_config(
                MasterRuntimeOptions(
                    AgentProvider.CLAUDE,
                    "master-model",
                    "high",
                )
            )

            target = persist_master_runtime_snapshot(
                control,
                "run-1",
                stored,
                None,
            )
            self.assertEqual(control / "master-runtime.json", target)
            self.assertEqual(
                stored,
                load_master_runtime_snapshot(root, "run-1"),
            )

            self.assertEqual(
                target,
                persist_master_runtime_snapshot(
                    control,
                    "run-1",
                    stored,
                    None,
                ),
            )

            changed = build_master_runtime_config(
                MasterRuntimeOptions(
                    AgentProvider.CODEX,
                    "different-master-model",
                    None,
                )
            )
            with self.assertRaisesRegex(
                Exception,
                "already exists with different content",
            ):
                persist_master_runtime_snapshot(
                    control,
                    "run-1",
                    changed,
                    None,
                )

    def test_legacy_resume_reports_snapshot_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(root, resume=True)
            stderr = io.StringIO()
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    "1.4.159",
                    "a" * 40,
                ),
                interactive=False,
                stderr=stderr,
            )
            self.assertEqual(
                default_agent_runtime_config(),
                prepared.agent_runtime,
            )
            self.assertIn("migration snapshot", stderr.getvalue())

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
            request.write_text("request", encoding="utf-8")
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
                "clean",
            ):
                run_preflight(arguments, client)  # type: ignore[arg-type]

    def test_preflight_accepts_live_status_shape(
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
            request.write_text("request", encoding="utf-8")
            subprocess.run(
                ("git", "add", "request.md"),
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
    def _master_preflight(self, root: Path) -> PreflightResult:
        request = root / "request.md"
        request.write_text("change source directly", encoding="utf-8")
        prompts = root / "prompts"
        prompts.mkdir()
        (prompts / "master.md").write_text("# Master\n", encoding="utf-8")
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
            ("git", "add", "request.md", "prompts/master.md"),
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
            ),
            harness_root=root,
        )
        master_runtime = build_master_runtime_config(
            MasterRuntimeOptions(
                provider=AgentProvider.CLAUDE,
                model="master-model",
                effort="high",
            )
        )
        return PreflightResult(
            arguments=arguments,
            test_policy=empty_test_policy(),
            orca_version="1.4.159",
            base_head="a" * 40,
            master_runtime=master_runtime,
        )

    def _provision_client(self) -> FakeOrcaClient:
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
            raise AssertionError(f"unexpected Orca call: {argv}")

        return FakeOrcaClient(handler)

    def test_initialize_records_four_workers_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            request.write_text("request", encoding="utf-8")
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
                ("git", "add", "request.md"),
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
                ),
                harness_root=root,
            )
            preflight = PreflightResult(
                arguments,
                empty_test_policy(),
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
            runtime_snapshot = (
                controller.workspace.control_dir / "agent-runtime.json"
            )
            self.assertTrue(runtime_snapshot.is_file())

    def test_initial_master_can_dispatch_planner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            observed_prompt = []

            def master_invoke(_options, prompt):
                observed_prompt.append(prompt)
                return (
                    '{"action":"dispatch","role":"planner",'
                    '"permissionProfile":"read_only",'
                    '"reason":"planning is required"}'
                )

            controller, pool = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=master_invoke,
            )
            self.assertEqual(LoopState.PLAN, controller.state.state)
            self.assertEqual(4, len(pool.workers))
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"currentState":"INIT"', observed_prompt[0])
            self.assertIn('"request":"change source directly"', observed_prompt[0])
            self.assertIn('"role":"planner"', observed_prompt[0])
            self.assertIn('"role":"implementer"', observed_prompt[0])

    def test_initial_master_can_dispatch_implementer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"the request is directly actionable"}'
                ),
            )
            self.assertEqual(LoopState.IMPLEMENT, controller.state.state)

    def test_initial_master_rejects_invalid_role_permission_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            with self.assertRaisesRegex(
                OrcaLoopError,
                "permission profile is not allowed for role",
            ):
                _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"dispatch","role":"planner",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"invalid permission request"}'
                    ),
                )

    def test_initial_master_rejects_other_valid_worker_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            with self.assertRaisesRegex(
                OrcaLoopError,
                "initial Master routing role is not allowed: code_reviewer",
            ):
                _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"dispatch","role":"code_reviewer",'
                        '"permissionProfile":"read_only",'
                        '"reason":"review immediately"}'
                    ),
                )

    def test_initial_master_rejects_non_dispatch_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            with self.assertRaisesRegex(
                OrcaLoopError,
                "initial Master routing requires action dispatch",
            ):
                _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"finish","role":null,'
                        '"permissionProfile":null,'
                        '"reason":"nothing to do"}'
                    ),
                )

    def test_worker_completion_master_policy_matches_fixed_next_steps(self) -> None:
        self.assertIsNone(
            _validate_worker_completion_master_decision(
                LoopState.PLAN,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.PLAN_REVIEWER,
                    PermissionProfile.READ_ONLY,
                    "review the plan",
                ),
            )
        )
        self.assertIsNone(
            _validate_worker_completion_master_decision(
                LoopState.IMPLEMENT,
                MasterDecision(
                    MasterAction.TEST,
                    None,
                    None,
                    "run the test gate",
                ),
            )
        )
        self.assertIsNone(
            _validate_worker_completion_master_decision(
                LoopState.CODE_REVIEW,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.CROSS_CONFIRMER,
                    PermissionProfile.READ_ONLY,
                    "cross-confirm the review",
                ),
            )
        )
        self.assertEqual(
            SignalKind.ESCALATE,
            _validate_worker_completion_master_decision(
                LoopState.IMPLEMENT,
                MasterDecision(
                    MasterAction.ESCALATE,
                    None,
                    None,
                    "human decision required",
                ),
            ),
        )
        self.assertEqual(
            LoopState.IMPLEMENT,
            _validate_worker_completion_master_decision(
                LoopState.PLAN,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.IMPLEMENTER,
                    PermissionProfile.WORKSPACE_WRITE,
                    "safe plan can proceed directly",
                ),
                allow_direct_implementation=True,
            ),
        )

    def test_worker_completion_master_rejects_state_machine_jump(self) -> None:
        with self.assertRaisesRegex(
            OrcaLoopError,
            "after planning must dispatch plan_reviewer or escalate",
        ):
            _validate_worker_completion_master_decision(
                LoopState.PLAN,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.IMPLEMENTER,
                    PermissionProfile.WORKSPACE_WRITE,
                    "skip review",
                ),
            )
        with self.assertRaisesRegex(
            OrcaLoopError,
            "after implementation must request test or escalate",
        ):
            _validate_worker_completion_master_decision(
                LoopState.IMPLEMENT,
                MasterDecision(
                    MasterAction.FINISH,
                    None,
                    None,
                    "finish early",
                ),
            )

    def test_worker_completion_master_routes_plan_through_existing_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"planner",'
                    '"permissionProfile":"read_only",'
                    '"reason":"planning is required"}'
                ),
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "plan artifact accepted",
                    (),
                ),
                controller.ledger,
                None,
            )
            observed_prompt = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.PLANNER,
                result,
                {"plan_version": 1},
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"dispatch","role":"plan_reviewer",'
                    '"permissionProfile":"read_only",'
                    '"reason":"review the verified plan"}'
                ),
            )
            self.assertIs(routed, result)
            commit_step_transition(
                controller,
                routed,
                preflight.arguments.config,
            )
            self.assertEqual(LoopState.PLAN_REVIEW, controller.state.state)
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"stage":"worker_completion"', observed_prompt[0])
            self.assertIn('"completedRole":"planner"', observed_prompt[0])
            self.assertIn('"action":"dispatch"', observed_prompt[0])
            self.assertIn('"role":"plan_reviewer"', observed_prompt[0])

    def test_worker_completion_master_can_escalate_without_changing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "implementation artifact accepted",
                    (),
                ),
                controller.ledger,
                None,
            )
            routed = _route_worker_completion(
                controller,
                preflight,
                Role.IMPLEMENTER,
                result,
                {"status": "implemented"},
                master_invoke=lambda _options, _prompt: (
                    '{"action":"escalate","role":null,'
                    '"permissionProfile":null,'
                    '"reason":"human review required"}'
                ),
            )
            self.assertEqual(SignalKind.ESCALATE, routed.signal.kind)
            self.assertIn("human review required", routed.signal.reason)

    def test_worker_completion_master_can_skip_plan_review_for_safe_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"planner",'
                    '"permissionProfile":"read_only",'
                    '"reason":"planning is required"}'
                ),
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "plan artifact accepted",
                    (),
                ),
                controller.ledger,
                None,
            )
            observed_prompt = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.PLANNER,
                result,
                coordinator_plan(),
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"verified plan is low risk"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.IMPLEMENT, controller.state.state)
            self.assertEqual(
                "TRANSITION_COMMITTED",
                controller.state.step_stage.value,
            )
            self.assertIn(
                "skipped plan review for safe verified plan",
                controller.state.history[-1].reason,
            )
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"role":"plan_reviewer"', observed_prompt[0])
            self.assertIn('"role":"implementer"', observed_prompt[0])

    def test_worker_completion_master_cannot_skip_plan_review_for_risky_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"planner",'
                    '"permissionProfile":"read_only",'
                    '"reason":"planning is required"}'
                ),
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "plan artifact accepted",
                    (),
                ),
                controller.ledger,
                None,
            )
            risky_plan = replace(
                coordinator_plan(),
                data_api_schema_changes="API contract changes",
            )
            observed_prompt = []

            with self.assertRaisesRegex(
                OrcaLoopError,
                "must dispatch plan_reviewer or escalate",
            ):
                _route_worker_completion(
                    controller,
                    preflight,
                    Role.PLANNER,
                    result,
                    risky_plan,
                    master_invoke=lambda _options, prompt: (
                        observed_prompt.append(prompt)
                        or '{"action":"dispatch","role":"implementer",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"try to skip required review"}'
                    ),
                )

            self.assertEqual(LoopState.PLAN, controller.state.state)
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"role":"plan_reviewer"', observed_prompt[0])
            self.assertNotIn('"role":"implementer"', observed_prompt[0])

    def test_worker_completion_master_can_finish_clean_code_review_through_human_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.CODE_REVIEW,
                test_gate_status=GateStatus.PASS,
            )
            (controller.workspace.artifact_dir / "plan.json").write_text(
                serialize_json(coordinator_plan()),
                encoding="utf-8",
            )
            review = ReviewArtifact(
                schema_version=1,
                artifact_kind=ArtifactKind.CODE_REVIEW,
                run_id="run-1",
                task_id="task-review",
                dispatch_id="dispatch-review",
                consensus_round=1,
                snapshot_digest=controller.state.snapshot_digest,
                role=Role.CODE_REVIEWER,
                verdict=CodeReviewVerdict.APPROVE,
                reviewed_plan_version=controller.state.plan_version,
                reviewed_artifact_digest="sha256:" + "b" * 64,
                reviewed_finding_ids=(),
                finding_decisions=(),
                findings=(),
                non_blocking_suggestions=(),
                escalation_signals=(),
                agrees_with_reviewer=None,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "clean code review accepted",
                    (),
                ),
                controller.ledger,
                GateStatus.PASS,
            )
            observed_prompt = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.CODE_REVIEWER,
                result,
                review,
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"finish","role":null,'
                    '"permissionProfile":null,'
                    '"reason":"clean low-risk review can go to human disposition"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.HUMAN_GATE, controller.state.state)
            self.assertEqual(
                "TRANSITION_COMMITTED",
                controller.state.step_stage.value,
            )
            self.assertEqual(GateStatus.PASS, controller.state.test_gate_status)
            self.assertIn(
                "skipped cross-confirm for clean low-risk code review",
                controller.state.history[-1].reason,
            )
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"role":"cross_confirmer"', observed_prompt[0])
            self.assertIn('"action":"finish"', observed_prompt[0])
            self.assertIn('"action":"escalate"', observed_prompt[0])

    def test_worker_completion_master_cannot_finish_nonclean_or_risky_code_review(
        self,
    ) -> None:
        clean_review = ReviewArtifact(
            schema_version=1,
            artifact_kind=ArtifactKind.CODE_REVIEW,
            run_id="run-1",
            task_id="task-review",
            dispatch_id="dispatch-review",
            consensus_round=1,
            snapshot_digest="sha256:" + "a" * 64,
            role=Role.CODE_REVIEWER,
            verdict=CodeReviewVerdict.APPROVE,
            reviewed_plan_version=0,
            reviewed_artifact_digest="sha256:" + "b" * 64,
            reviewed_finding_ids=(),
            finding_decisions=(),
            findings=(),
            non_blocking_suggestions=(),
            escalation_signals=(),
            agrees_with_reviewer=None,
        )
        nonclean_reviews = (
            replace(clean_review, verdict=CodeReviewVerdict.CHANGES_REQUESTED),
            replace(clean_review, reviewed_finding_ids=("F-1",)),
            replace(
                clean_review,
                finding_decisions=(
                    ledger_decision(
                        "F-1",
                        Side.CODEX,
                        DecisionValue.APPROVE,
                        1,
                    ),
                ),
            ),
            replace(clean_review, findings=(ledger_finding(),)),
            replace(
                clean_review,
                non_blocking_suggestions=(
                    InformationalFinding(
                        finding_id="INFO-1",
                        description="minor suggestion",
                        evidence_refs=("review.json",),
                    ),
                ),
            ),
            replace(
                clean_review,
                escalation_signals=(
                    EscalationTrigger(
                        code=EscalationCode.E01,
                        reason="requires human decision",
                        evidence_refs=("review.json",),
                        deduplication_key="phase13-review-escalation",
                    ),
                ),
            ),
        )

        cases = (
            ("not_passed", clean_review, GateStatus.NOT_RUN, coordinator_plan(), ()),
            *(
                (f"nonclean_{index}", review, GateStatus.PASS, coordinator_plan(), ())
                for index, review in enumerate(nonclean_reviews)
            ),
            (
                "result_escalation",
                clean_review,
                GateStatus.PASS,
                coordinator_plan(),
                (
                    EscalationTrigger(
                        code=EscalationCode.E01,
                        reason="result requires escalation",
                        evidence_refs=("result",),
                        deduplication_key="phase13-result-escalation",
                    ),
                ),
            ),
            (
                "risky_schema_plan",
                clean_review,
                GateStatus.PASS,
                replace(
                    coordinator_plan(),
                    data_api_schema_changes="schema migration",
                ),
                (),
            ),
        )

        for name, review, test_status, plan, escalations in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                preflight = self._master_preflight(root)
                controller, _ = _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"dispatch","role":"implementer",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"implement directly"}'
                    ),
                )
                controller.state = replace(
                    controller.state,
                    state=LoopState.CODE_REVIEW,
                    test_gate_status=test_status,
                )
                (controller.workspace.artifact_dir / "plan.json").write_text(
                    serialize_json(plan),
                    encoding="utf-8",
                )
                result = StepExecutionResult(
                    TransitionSignal(
                        SignalKind.ARTIFACT_OK,
                        "code review accepted",
                        (),
                    ),
                    controller.ledger,
                    test_status,
                    escalations,
                )
                observed_prompt = []

                with self.assertRaisesRegex(
                    OrcaLoopError,
                    "must dispatch cross_confirmer or escalate",
                ):
                    _route_worker_completion(
                        controller,
                        preflight,
                        Role.CODE_REVIEWER,
                        result,
                        review,
                        master_invoke=lambda _options, prompt: (
                            observed_prompt.append(prompt)
                            or '{"action":"finish","role":null,'
                            '"permissionProfile":null,'
                            '"reason":"try to skip required cross-confirmation"}'
                        ),
                    )

                self.assertEqual(LoopState.CODE_REVIEW, controller.state.state)
                self.assertEqual(1, len(observed_prompt))
                self.assertIn('"role":"cross_confirmer"', observed_prompt[0])
                self.assertNotIn('"action":"finish"', observed_prompt[0])

    def test_worker_completion_master_can_collapse_clean_cross_confirm_consensus(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.CROSS_CONFIRM,
                test_gate_status=GateStatus.PASS,
            )
            artifacts = controller.workspace.artifact_dir
            artifacts.joinpath("plan.json").write_text(
                serialize_json(coordinator_plan()),
                encoding="utf-8",
            )
            artifacts.joinpath("code_review.json").write_text(
                '{"review":"clean"}',
                encoding="utf-8",
            )
            artifacts.joinpath("cross_review.json").write_text(
                '{"cross":"clean"}',
                encoding="utf-8",
            )
            review = ReviewArtifact(
                schema_version=1,
                artifact_kind=ArtifactKind.CROSS_REVIEW,
                run_id="run-1",
                task_id="task-cross",
                dispatch_id="dispatch-cross",
                consensus_round=1,
                snapshot_digest=controller.state.snapshot_digest,
                role=Role.CROSS_CONFIRMER,
                verdict=CodeReviewVerdict.APPROVE,
                reviewed_plan_version=controller.state.plan_version,
                reviewed_artifact_digest="sha256:" + "c" * 64,
                reviewed_finding_ids=(),
                finding_decisions=(),
                findings=(),
                non_blocking_suggestions=(),
                escalation_signals=(),
                agrees_with_reviewer=True,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "cross-confirm accepted",
                    (),
                ),
                controller.ledger,
                GateStatus.PASS,
            )
            observed_prompt = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.CROSS_CONFIRMER,
                result,
                review,
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"finish","role":null,'
                    '"permissionProfile":null,'
                    '"reason":"verified consensus can go to human disposition"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.HUMAN_GATE, controller.state.state)
            self.assertEqual(
                "TRANSITION_COMMITTED",
                controller.state.step_stage.value,
            )
            self.assertEqual(1, controller.ledger.code_round)
            self.assertEqual(GateStatus.PASS, controller.state.test_gate_status)
            self.assertIn(
                "collapsed clean cross-confirm consensus evaluation",
                controller.state.history[-1].reason,
            )
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"currentState":"CROSS_CONFIRM"', observed_prompt[0])
            self.assertIn('"action":"finish"', observed_prompt[0])
            self.assertIn('"action":"escalate"', observed_prompt[0])

    def test_worker_completion_master_can_collapse_clean_plan_review_consensus(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            plan = coordinator_plan()
            controller.state = replace(
                controller.state,
                state=LoopState.PLAN_REVIEW,
                plan_version=plan.plan_version,
            )
            artifacts = controller.workspace.artifact_dir
            artifacts.joinpath("plan.json").write_text(
                serialize_json(plan),
                encoding="utf-8",
            )
            artifacts.joinpath("plan_review.json").write_text(
                '{"review":"clean"}',
                encoding="utf-8",
            )
            review = ReviewArtifact(
                schema_version=1,
                artifact_kind=ArtifactKind.PLAN_REVIEW,
                run_id="run-1",
                task_id="task-plan-review",
                dispatch_id="dispatch-plan-review",
                consensus_round=1,
                snapshot_digest=controller.state.snapshot_digest,
                role=Role.PLAN_REVIEWER,
                verdict=PlanReviewVerdict.APPROVE,
                reviewed_plan_version=plan.plan_version,
                reviewed_artifact_digest="sha256:" + "b" * 64,
                reviewed_finding_ids=(),
                finding_decisions=(),
                findings=(),
                non_blocking_suggestions=(),
                escalation_signals=(),
                agrees_with_reviewer=None,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "plan review accepted",
                    (),
                ),
                controller.ledger,
                controller.state.test_gate_status,
            )
            observed_prompt = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.PLAN_REVIEWER,
                result,
                review,
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"verified plan consensus can proceed"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.IMPLEMENT, controller.state.state)
            self.assertEqual(
                "TRANSITION_COMMITTED",
                controller.state.step_stage.value,
            )
            self.assertEqual(1, controller.ledger.plan_round)
            self.assertIn(
                "collapsed clean plan-review consensus evaluation",
                controller.state.history[-1].reason,
            )
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"currentState":"PLAN_REVIEW"', observed_prompt[0])
            self.assertIn('"role":"implementer"', observed_prompt[0])
            self.assertIn('"permissionProfile":"workspace_write"', observed_prompt[0])
            self.assertIn('"action":"escalate"', observed_prompt[0])
            self.assertNotIn('"action":"finish"', observed_prompt[0])

    def test_worker_completion_master_keeps_plan_consensus_evaluate_for_ineligible_plan_review(
        self,
    ) -> None:
        clean_review = ReviewArtifact(
            schema_version=1,
            artifact_kind=ArtifactKind.PLAN_REVIEW,
            run_id="run-1",
            task_id="task-plan-review",
            dispatch_id="dispatch-plan-review",
            consensus_round=1,
            snapshot_digest="sha256:" + "a" * 64,
            role=Role.PLAN_REVIEWER,
            verdict=PlanReviewVerdict.APPROVE,
            reviewed_plan_version=1,
            reviewed_artifact_digest="sha256:" + "b" * 64,
            reviewed_finding_ids=(),
            finding_decisions=(),
            findings=(),
            non_blocking_suggestions=(),
            escalation_signals=(),
            agrees_with_reviewer=None,
        )
        cases = (
            (
                "suggestion",
                replace(
                    clean_review,
                    non_blocking_suggestions=(
                        InformationalFinding(
                            finding_id="INFO-PLAN",
                            description="retain plan consensus stage",
                            evidence_refs=("plan-review.json",),
                        ),
                    ),
                ),
                coordinator_plan(),
                True,
            ),
            (
                "api_schema_change",
                clean_review,
                replace(
                    coordinator_plan(),
                    data_api_schema_changes="API contract change",
                ),
                True,
            ),
            (
                "destructive_delete",
                clean_review,
                replace(
                    coordinator_plan(),
                    affected_files=(
                        AffectedFile(
                            "src/obsolete.py",
                            AffectedFileOperation.DELETE,
                            None,
                        ),
                    ),
                ),
                True,
            ),
            (
                "missing_round_evidence",
                clean_review,
                coordinator_plan(),
                False,
            ),
        )

        for name, review, plan, write_review_evidence in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                preflight = self._master_preflight(root)
                controller, _ = _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"dispatch","role":"implementer",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"implement directly"}'
                    ),
                )
                controller.state = replace(
                    controller.state,
                    state=LoopState.PLAN_REVIEW,
                    plan_version=plan.plan_version,
                )
                artifacts = controller.workspace.artifact_dir
                artifacts.joinpath("plan.json").write_text(
                    serialize_json(plan),
                    encoding="utf-8",
                )
                if write_review_evidence:
                    artifacts.joinpath("plan_review.json").write_text(
                        '{"review":"clean"}',
                        encoding="utf-8",
                    )
                result = StepExecutionResult(
                    TransitionSignal(
                        SignalKind.ARTIFACT_OK,
                        "plan review accepted",
                        (),
                    ),
                    controller.ledger,
                    controller.state.test_gate_status,
                )
                master_calls = []

                routed = _route_worker_completion(
                    controller,
                    preflight,
                    Role.PLAN_REVIEWER,
                    result,
                    review,
                    master_invoke=lambda _options, prompt: (
                        master_calls.append(prompt)
                        or '{"action":"dispatch","role":"implementer",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"should not be called"}'
                    ),
                )

                self.assertIs(routed, result)
                self.assertEqual(LoopState.PLAN_REVIEW, controller.state.state)
                self.assertEqual(0, controller.ledger.plan_round)
                self.assertEqual([], master_calls)
                commit_step_transition(
                    controller,
                    routed,
                    preflight.arguments.config,
                )
                self.assertEqual(
                    LoopState.PLAN_CONSENSUS_EVALUATE,
                    controller.state.state,
                )

    def test_worker_completion_master_keeps_plan_consensus_evaluate_when_preview_hits_round_limit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            plan = coordinator_plan()
            controller.state = replace(
                controller.state,
                state=LoopState.PLAN_REVIEW,
                plan_version=plan.plan_version,
            )
            artifacts = controller.workspace.artifact_dir
            artifacts.joinpath("plan.json").write_text(
                serialize_json(plan),
                encoding="utf-8",
            )
            artifacts.joinpath("plan_review.json").write_text(
                '{"review":"clean"}',
                encoding="utf-8",
            )
            review = ReviewArtifact(
                schema_version=1,
                artifact_kind=ArtifactKind.PLAN_REVIEW,
                run_id="run-1",
                task_id="task-plan-review",
                dispatch_id="dispatch-plan-review",
                consensus_round=1,
                snapshot_digest=controller.state.snapshot_digest,
                role=Role.PLAN_REVIEWER,
                verdict=PlanReviewVerdict.APPROVE,
                reviewed_plan_version=plan.plan_version,
                reviewed_artifact_digest="sha256:" + "b" * 64,
                reviewed_finding_ids=(),
                finding_decisions=(),
                findings=(),
                non_blocking_suggestions=(),
                escalation_signals=(),
                agrees_with_reviewer=None,
            )
            exhausted_ledger = replace(
                controller.ledger,
                plan_round=preflight.arguments.config.plan_consensus_round_limit,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.ARTIFACT_OK,
                    "plan review accepted",
                    (),
                ),
                exhausted_ledger,
                controller.state.test_gate_status,
            )
            master_calls = []

            routed = _route_worker_completion(
                controller,
                preflight,
                Role.PLAN_REVIEWER,
                result,
                review,
                master_invoke=lambda _options, prompt: (
                    master_calls.append(prompt)
                    or '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"should not be called"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.PLAN_REVIEW, controller.state.state)
            self.assertEqual([], master_calls)
            commit_step_transition(
                controller,
                routed,
                preflight.arguments.config,
            )
            self.assertEqual(
                LoopState.PLAN_CONSENSUS_EVALUATE,
                controller.state.state,
            )

    def test_worker_completion_master_keeps_consensus_evaluate_for_ineligible_cross_confirm(
        self,
    ) -> None:
        clean_review = ReviewArtifact(
            schema_version=1,
            artifact_kind=ArtifactKind.CROSS_REVIEW,
            run_id="run-1",
            task_id="task-cross",
            dispatch_id="dispatch-cross",
            consensus_round=1,
            snapshot_digest="sha256:" + "a" * 64,
            role=Role.CROSS_CONFIRMER,
            verdict=CodeReviewVerdict.APPROVE,
            reviewed_plan_version=0,
            reviewed_artifact_digest="sha256:" + "c" * 64,
            reviewed_finding_ids=(),
            finding_decisions=(),
            findings=(),
            non_blocking_suggestions=(),
            escalation_signals=(),
            agrees_with_reviewer=True,
        )
        cases = (
            ("not_passed", clean_review, GateStatus.NOT_RUN, coordinator_plan()),
            (
                "disagrees",
                replace(clean_review, agrees_with_reviewer=False),
                GateStatus.PASS,
                coordinator_plan(),
            ),
            (
                "suggestion",
                replace(
                    clean_review,
                    non_blocking_suggestions=(
                        InformationalFinding(
                            finding_id="INFO-X",
                            description="keep cross consensus stage",
                            evidence_refs=("cross-review.json",),
                        ),
                    ),
                ),
                GateStatus.PASS,
                coordinator_plan(),
            ),
            (
                "risky_plan",
                clean_review,
                GateStatus.PASS,
                replace(
                    coordinator_plan(),
                    data_api_schema_changes="API contract change",
                ),
            ),
        )

        for name, review, test_status, plan in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                preflight = self._master_preflight(root)
                controller, _ = _initialize(
                    preflight,
                    self._provision_client(),  # type: ignore[arg-type]
                    master_invoke=lambda _options, _prompt: (
                        '{"action":"dispatch","role":"implementer",'
                        '"permissionProfile":"workspace_write",'
                        '"reason":"implement directly"}'
                    ),
                )
                controller.state = replace(
                    controller.state,
                    state=LoopState.CROSS_CONFIRM,
                    test_gate_status=test_status,
                )
                artifacts = controller.workspace.artifact_dir
                artifacts.joinpath("plan.json").write_text(
                    serialize_json(plan),
                    encoding="utf-8",
                )
                artifacts.joinpath("code_review.json").write_text(
                    '{"review":"clean"}',
                    encoding="utf-8",
                )
                artifacts.joinpath("cross_review.json").write_text(
                    '{"cross":"clean"}',
                    encoding="utf-8",
                )
                result = StepExecutionResult(
                    TransitionSignal(
                        SignalKind.ARTIFACT_OK,
                        "cross-confirm accepted",
                        (),
                    ),
                    controller.ledger,
                    test_status,
                )
                master_calls = []

                routed = _route_worker_completion(
                    controller,
                    preflight,
                    Role.CROSS_CONFIRMER,
                    result,
                    review,
                    master_invoke=lambda _options, prompt: (
                        master_calls.append(prompt)
                        or '{"action":"finish","role":null,'
                        '"permissionProfile":null,'
                        '"reason":"should not be called"}'
                    ),
                )

                self.assertIs(routed, result)
                self.assertEqual(LoopState.CROSS_CONFIRM, controller.state.state)
                self.assertEqual(0, controller.ledger.code_round)
                self.assertEqual([], master_calls)
                commit_step_transition(
                    controller,
                    routed,
                    preflight.arguments.config,
                )
                self.assertEqual(
                    LoopState.CONSENSUS_EVALUATE,
                    controller.state.state,
                )

    def test_test_result_master_policy_matches_fixed_transitions(self) -> None:
        self.assertIsNone(
            _validate_test_result_master_decision(
                SignalKind.PASS,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.CODE_REVIEWER,
                    PermissionProfile.READ_ONLY,
                    "review passing implementation",
                ),
            )
        )
        self.assertIsNone(
            _validate_test_result_master_decision(
                SignalKind.NOT_RUN,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.CODE_REVIEWER,
                    PermissionProfile.READ_ONLY,
                    "review when tests are not required",
                ),
            )
        )
        self.assertIsNone(
            _validate_test_result_master_decision(
                SignalKind.FAIL,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.IMPLEMENTER,
                    PermissionProfile.WORKSPACE_WRITE,
                    "fix the failed tests",
                ),
            )
        )
        self.assertEqual(
            SignalKind.ESCALATE,
            _validate_test_result_master_decision(
                SignalKind.FAIL,
                MasterDecision(
                    MasterAction.ESCALATE,
                    None,
                    None,
                    "failure needs a human decision",
                ),
            ),
        )
        self.assertEqual(
            LoopState.HUMAN_GATE,
            _validate_test_result_master_decision(
                SignalKind.PASS,
                MasterDecision(
                    MasterAction.FINISH,
                    None,
                    None,
                    "safe passing change is ready for final human disposition",
                ),
                allow_finish=True,
            ),
        )

    def test_test_result_master_rejects_skipping_safety_boundaries(self) -> None:
        with self.assertRaisesRegex(
            OrcaLoopError,
            "successful test gate must dispatch code_reviewer or escalate",
        ):
            _validate_test_result_master_decision(
                SignalKind.PASS,
                MasterDecision(
                    MasterAction.FINISH,
                    None,
                    None,
                    "finish without review",
                ),
            )
        with self.assertRaisesRegex(
            OrcaLoopError,
            "failed test gate must dispatch implementer or escalate",
        ):
            _validate_test_result_master_decision(
                SignalKind.FAIL,
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.PLANNER,
                    PermissionProfile.READ_ONLY,
                    "skip directly to redesign",
                ),
            )

    def test_test_result_master_routes_pass_with_verified_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.TEST_GATE,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.PASS,
                    "test gate result: PASS",
                    (),
                ),
                controller.ledger,
                None,
            )
            observed_prompt = []
            routed = _route_test_result(
                controller,
                preflight,
                result,
                {"plan_version": 1},  # type: ignore[arg-type]
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"dispatch","role":"code_reviewer",'
                    '"permissionProfile":"read_only",'
                    '"reason":"review after tests pass"}'
                ),
            )
            self.assertIs(routed, result)
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"stage":"test_result"', observed_prompt[0])
            self.assertIn('"resultSignal":"PASS"', observed_prompt[0])
            self.assertIn('"role":"code_reviewer"', observed_prompt[0])

    def test_test_result_master_can_finish_safe_passing_change_through_human_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.TEST_GATE,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.PASS,
                    "test gate result: PASS",
                    (),
                ),
                controller.ledger,
                GateStatus.PASS,
            )
            observed_prompt = []

            routed = _route_test_result(
                controller,
                preflight,
                result,
                coordinator_plan(),
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"finish","role":null,'
                    '"permissionProfile":null,'
                    '"reason":"safe passing change can go to final human gate"}'
                ),
            )

            self.assertIs(routed, result)
            self.assertEqual(LoopState.HUMAN_GATE, controller.state.state)
            self.assertEqual(
                "TRANSITION_COMMITTED",
                controller.state.step_stage.value,
            )
            self.assertEqual(GateStatus.PASS, controller.state.test_gate_status)
            self.assertIn(
                "skipped code review for safe passing change",
                controller.state.history[-1].reason,
            )
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"role":"code_reviewer"', observed_prompt[0])
            self.assertIn('"action":"finish"', observed_prompt[0])

    def test_test_result_master_cannot_finish_without_verified_pass_or_safe_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.TEST_GATE,
            )
            unsafe_results = (
                StepExecutionResult(
                    TransitionSignal(
                        SignalKind.NOT_RUN,
                        "test gate result: NOT_RUN",
                        (),
                    ),
                    controller.ledger,
                    GateStatus.NOT_RUN,
                ),
                StepExecutionResult(
                    TransitionSignal(
                        SignalKind.PASS,
                        "test gate result: PASS",
                        (),
                    ),
                    controller.ledger,
                    GateStatus.NOT_RUN,
                ),
            )
            risky_plan = replace(
                coordinator_plan(),
                data_api_schema_changes="schema migration",
            )

            for result, plan in (
                (unsafe_results[0], coordinator_plan()),
                (unsafe_results[1], coordinator_plan()),
                (
                    StepExecutionResult(
                        TransitionSignal(
                            SignalKind.PASS,
                            "test gate result: PASS",
                            (),
                        ),
                        controller.ledger,
                        GateStatus.PASS,
                    ),
                    risky_plan,
                ),
            ):
                observed_prompt = []
                with self.assertRaisesRegex(
                    OrcaLoopError,
                    "must dispatch code_reviewer or escalate",
                ):
                    _route_test_result(
                        controller,
                        preflight,
                        result,
                        plan,
                        master_invoke=lambda _options, prompt: (
                            observed_prompt.append(prompt)
                            or '{"action":"finish","role":null,'
                            '"permissionProfile":null,'
                            '"reason":"attempt unsafe shortcut"}'
                        ),
                    )
                self.assertEqual(LoopState.TEST_GATE, controller.state.state)
                self.assertEqual(1, len(observed_prompt))
                self.assertIn('"role":"code_reviewer"', observed_prompt[0])
                self.assertNotIn('"action":"finish"', observed_prompt[0])

    def test_test_result_policy_violation_bypasses_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.TEST_GATE,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.POLICY_VIOLATION,
                    "test gate result: POLICY_VIOLATION",
                    (),
                ),
                controller.ledger,
                None,
            )
            called = []
            routed = _route_test_result(
                controller,
                preflight,
                result,
                {"plan_version": 1},  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: called.append(True) or "",
            )
            self.assertIs(routed, result)
            self.assertEqual([], called)

    def test_final_master_policy_preserves_human_gate_boundary(self) -> None:
        self.assertIsNone(
            _validate_final_master_decision(
                MasterDecision(
                    MasterAction.FINISH,
                    None,
                    None,
                    "evidence is sufficient for final human disposition",
                )
            )
        )
        self.assertEqual(
            SignalKind.ESCALATE,
            _validate_final_master_decision(
                MasterDecision(
                    MasterAction.ESCALATE,
                    None,
                    None,
                    "human attention is required before the final gate",
                )
            ),
        )

    def test_final_master_rejects_state_machine_jump(self) -> None:
        with self.assertRaisesRegex(
            OrcaLoopError,
            "must finish through the human gate or escalate",
        ):
            _validate_final_master_decision(
                MasterDecision(
                    MasterAction.DISPATCH,
                    Role.IMPLEMENTER,
                    PermissionProfile.WORKSPACE_WRITE,
                    "skip the final boundary",
                )
            )
        with self.assertRaisesRegex(
            OrcaLoopError,
            "must finish through the human gate or escalate",
        ):
            _validate_final_master_decision(
                MasterDecision(
                    MasterAction.ABORT,
                    None,
                    None,
                    "abort instead of final disposition",
                )
            )

    def test_final_master_routes_finish_with_verified_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            preflight = self._master_preflight(root)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
                master_invoke=lambda _options, _prompt: (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"implement directly"}'
                ),
            )
            controller.state = replace(
                controller.state,
                state=LoopState.CONSENSUS_EVALUATE,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.UNRESOLVED_ZERO,
                    "consensus reached",
                    (),
                ),
                controller.ledger,
                None,
            )
            observed_prompt = []
            routed = _route_final_decision(
                controller,
                preflight,
                result,
                None,
                master_invoke=lambda _options, prompt: (
                    observed_prompt.append(prompt)
                    or '{"action":"finish","role":null,'
                    '"permissionProfile":null,'
                    '"reason":"ready for the existing final human gate"}'
                ),
            )
            self.assertIs(routed, result)
            self.assertEqual(1, len(observed_prompt))
            self.assertIn('"stage":"final_decision"', observed_prompt[0])
            self.assertIn(
                '"currentState":"CONSENSUS_EVALUATE"',
                observed_prompt[0],
            )
            self.assertIn('"action":"finish"', observed_prompt[0])
            self.assertNotIn('"role":"implementer"', observed_prompt[0])

    def test_final_master_absent_and_nonfinal_results_keep_fixed_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            master_preflight = self._master_preflight(root)
            preflight = replace(master_preflight, master_runtime=None)
            controller, _ = _initialize(
                preflight,
                self._provision_client(),  # type: ignore[arg-type]
            )
            controller.state = replace(
                controller.state,
                state=LoopState.CONSENSUS_EVALUATE,
            )
            result = StepExecutionResult(
                TransitionSignal(
                    SignalKind.UNRESOLVED_ZERO,
                    "consensus reached",
                    (),
                ),
                controller.ledger,
                None,
            )
            called = []
            self.assertIs(
                result,
                _route_final_decision(
                    controller,
                    preflight,
                    result,
                    None,
                    master_invoke=lambda _options, _prompt: called.append(True) or "",
                ),
            )
            self.assertEqual([], called)

            unresolved = StepExecutionResult(
                TransitionSignal(
                    SignalKind.UNRESOLVED_REMAIN,
                    "unresolved findings remain",
                    (),
                ),
                controller.ledger,
                None,
            )
            self.assertIs(
                unresolved,
                _route_final_decision(
                    controller,
                    master_preflight,
                    unresolved,
                    None,
                    master_invoke=lambda _options, _prompt: called.append(True) or "",
                ),
            )
            self.assertEqual([], called)

    def test_resume_rejects_permission_policy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            request.write_text("request", encoding="utf-8")
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
                ("git", "add", "request.md"),
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
                ),
                harness_root=root,
            )
            preflight = PreflightResult(
                arguments,
                empty_test_policy(),
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

            _initialize(
                preflight,
                FakeOrcaClient(handler),  # type: ignore[arg-type]
            )
            resume_preflight = PreflightResult(
                parse_run_arguments(
                    (
                        "--run-id",
                        "run-1",
                        "--request",
                        str(request),
                        "--worktree",
                        str(root),
                        "--coordinator-handle",
                        "term-coordinator",
                        "--resume",
                    ),
                    harness_root=root,
                ),
                empty_test_policy(),
                "1.4.159",
                "a" * 40,
            )
            with patch(
                "run_loop.permission_policy_digest",
                return_value="sha256:" + "b" * 64,
            ):
                with self.assertRaisesRegex(
                    OrcaLoopError,
                    "resume permission policy does not match committed state",
                ):
                    _resume(resume_preflight)

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
