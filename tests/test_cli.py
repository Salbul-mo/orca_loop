from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.catalog import load_catalog
from orca_loop.config import (
    ConfigurationError,
    PreflightResult,
    default_agent_runtime_config,
    empty_test_policy,
    persist_agent_runtime_snapshot,
    persist_agent_runtime_source,
    PreflightError,
    parse_run_arguments,
    prepare_agent_runtime,
    resolve_agent_runtime,
    run_preflight,
)
from orca_loop.contracts import (
    build_agent_runtime_config,
    default_agent_provider,
    digest_value,
    parse_agent_runtime_config,
    serialize_agent_runtime_config,
)
from orca_loop.models import (
    AgentProvider,
    AgentRuntimeOptions,
    LoopState,
    PermissionCheck,
    PermissionFeasibilityReport,
    PermissionStrategy,
    RunStatus,
    ValidationStatus,
    WorkerKey,
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


def passing_permission_report(
    root: Path,
    *,
    include_claude_writable: bool = False,
) -> PermissionFeasibilityReport:
    check_count = 6 if include_claude_writable else 5
    return PermissionFeasibilityReport(
        schema_version=1,
        run_id="spike",
        status=ValidationStatus.PASS,
        strategy=PermissionStrategy.READONLY_REPOSITORY,
        checks=tuple(
            PermissionCheck(
                f"V-PERM-0{index}",
                ValidationStatus.PASS,
                ("evidence",),
            )
            for index in range(1, check_count + 1)
        ),
        evidence=("evidence",),
        orca_version="1.4.159",
        canonical_path=str(root / "permission.json"),
        report_digest="sha256:" + "a" * 64,
    )


class CliConfigurationTest(unittest.TestCase):
    def arguments(
        self,
        root: Path,
        *extra: str,
        resume: bool = False,
    ):
        request = root / "request.md"
        report = root / "permission.json"
        request.write_text("request", encoding="utf-8")
        report.write_text("{}", encoding="utf-8")
        values = [
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
            *extra,
        ]
        if resume:
            values.append("--resume")
        return parse_run_arguments(tuple(values), harness_root=root)

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

    def test_claude_implementer_requires_v_perm_06(self) -> None:
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
                passing_permission_report(root),
                "1.4.159",
                "a" * 40,
            )
            with self.assertRaisesRegex(PreflightError, "V-PERM-06"):
                prepare_agent_runtime(
                    base,
                    interactive=False,
                    stderr=io.StringIO(),
                )
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    passing_permission_report(
                        root,
                        include_claude_writable=True,
                    ),
                    "1.4.159",
                    "a" * 40,
                ),
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

    def static_catalog(self, root: Path):
        # `home=root` has no codex model cache, so the catalog falls back to
        # the built-in static tables and stays independent of the machine.
        return load_catalog(root, home=root)

    def test_agent_runtime_normalizes_requested_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(
                root,
                "--agent-model",
                "claude_planner=sonnet5",
                "--agent-effort",
                "claude_planner=mid",
                "--agent-model",
                "codex_implementer=terra",
                "--agent-effort",
                "codex_implementer=max",
            )
            stderr = io.StringIO()
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    passing_permission_report(root),
                    "1.4.159",
                    "a" * 40,
                ),
                interactive=False,
                stderr=stderr,
                catalog=self.static_catalog(root),
            )
            assert prepared.agent_runtime is not None
            by_worker = {
                item.worker_key: item
                for item in prepared.agent_runtime.agents
            }
            planner = by_worker[WorkerKey.CLAUDE_PLANNER]
            self.assertEqual("sonnet", planner.model)
            self.assertEqual("medium", planner.effort)
            implementer = by_worker[WorkerKey.CODEX_IMPLEMENTER]
            self.assertEqual("gpt-5.6-terra", implementer.model)
            self.assertEqual("max", implementer.effort)
            self.assertIn("from 'sonnet5'", stderr.getvalue())
            self.assertEqual(4, len(prepared.agent_resolutions))

    def test_unknown_model_falls_back_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(
                root,
                "--agent-model",
                "codex_review=gpt-9000",
            )
            stderr = io.StringIO()
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    passing_permission_report(root),
                    "1.4.159",
                    "a" * 40,
                ),
                interactive=False,
                stderr=stderr,
                catalog=self.static_catalog(root),
            )
            assert prepared.agent_runtime is not None
            review = {
                item.worker_key: item
                for item in prepared.agent_runtime.agents
            }[WorkerKey.CODEX_REVIEW]
            self.assertEqual("gpt-5.6-terra", review.model)
            self.assertIn("[WARN]", stderr.getvalue())
            self.assertIs(
                AgentProvider.CODEX,
                review.provider,
            )

    def test_strict_agent_runtime_rejects_unknown_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(
                root,
                "--strict-agent-runtime",
                "--agent-model",
                "codex_review=gpt-9000",
            )
            self.assertTrue(arguments.strict_agent_runtime)
            with self.assertRaisesRegex(
                ConfigurationError,
                "codex_review",
            ):
                prepare_agent_runtime(
                    PreflightResult(
                        arguments,
                        empty_test_policy(),
                        passing_permission_report(root),
                        "1.4.159",
                        "a" * 40,
                    ),
                    interactive=False,
                    stderr=io.StringIO(),
                    catalog=self.static_catalog(root),
                )

    def test_resume_accepts_equivalent_requested_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "runs" / "run-1" / "control"
            control.mkdir(parents=True)
            stored = build_agent_runtime_config(
                tuple(
                    AgentRuntimeOptions(
                        worker,
                        default_agent_provider(worker),
                        (
                            "sonnet"
                            if default_agent_provider(worker)
                            is AgentProvider.CLAUDE
                            else "gpt-5.6-terra"
                        ),
                        "medium",
                    )
                    for worker in WorkerKey
                )
            )
            persist_agent_runtime_snapshot(control, "run-1", stored, None)
            arguments = self.arguments(
                root,
                "--agent-model",
                "claude_planner=sonnet5",
                "--agent-effort",
                "claude_planner=mid",
                "--agent-model",
                "claude_code_review=sonnet",
                "--agent-effort",
                "claude_code_review=medium",
                "--agent-model",
                "codex_implementer=terra",
                "--agent-effort",
                "codex_implementer=medium",
                "--agent-model",
                "codex_review=terra",
                "--agent-effort",
                "codex_review=medium",
                resume=True,
            )
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    passing_permission_report(root),
                    "1.4.159",
                    "a" * 40,
                ),
                interactive=False,
                stderr=io.StringIO(),
                catalog=self.static_catalog(root),
            )
            self.assertEqual(stored, prepared.agent_runtime)

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
            report = passing_permission_report(root)
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    report,
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
                        report,
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
                        report,
                        "1.4.159",
                        "a" * 40,
                    ),
                    interactive=False,
                    stderr=io.StringIO(),
                )

    def test_legacy_resume_reports_snapshot_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self.arguments(root, resume=True)
            report = passing_permission_report(root)
            stderr = io.StringIO()
            prepared = prepare_agent_runtime(
                PreflightResult(
                    arguments,
                    empty_test_policy(),
                    report,
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
                if argv[:2] == ("orchestration", "run-create"):
                    return {"run": {"id": "orca-run-1"}}
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
