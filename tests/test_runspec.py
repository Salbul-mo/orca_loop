from __future__ import annotations

import json
from dataclasses import replace

import io
import tempfile
import unittest
from pathlib import Path

from orca_loop.catalog import load_catalog
from orca_loop.config import (
    PreflightResult,
    empty_test_policy,
    parse_run_arguments,
    prepare_agent_runtime,
)
from orca_loop.models import (
    AgentProvider,
    ConsensusIndependence,
    ConsensusProviderPolicy,
    PermissionCheck,
    PermissionFeasibilityReport,
    PermissionStrategy,
    ValidationStatus,
    WorkerHandle,
    WorkerKey,
    WorkerPool,
)
from orca_loop.runspec import (
    ManifestError,
    build_manifest,
    copy_request,
    manifest_to_arguments,
    parse_manifest,
    read_manifest,
    serialize_manifest,
    update_terminals,
    verify_inputs,
    worker_pool_from_manifest,
    write_manifest,
)


def permission_report(root: Path) -> PermissionFeasibilityReport:
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
            for index in range(1, 6)
        ),
        evidence=("evidence",),
        orca_version="1.4.159",
        canonical_path=str(root / "permission.json"),
        report_digest="sha256:" + "a" * 64,
    )


def sample_pool() -> WorkerPool:
    return WorkerPool(
        tuple(
            WorkerHandle(
                worker_key=worker,
                terminal_handle=f"term-{worker.value}",
                worktree_id="path:x",
                tab_id="",
                leaf_id="",
            )
            for worker in WorkerKey
        )
    )


class ManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / "runs" / "run-1" / "control"
        self.control.mkdir(parents=True)
        self.request = self.root / "request.md"
        self.request.write_text("작업 프롬프트 본문\n", encoding="utf-8")
        self.report = self.root / "permission.json"
        self.report.write_text("{}", encoding="utf-8")
        self.policy = self.root / "test-policy.json"
        self.policy.write_text("{}", encoding="utf-8")

    def preflight(self, *extra: str) -> PreflightResult:
        arguments = parse_run_arguments(
            (
                "--run-id",
                "run-1",
                "--request",
                str(self.request),
                "--worktree",
                str(self.root),
                "--coordinator-handle",
                "term-coordinator",
                "--permission-report",
                str(self.report),
                "--test-policy",
                str(self.policy),
                *extra,
            ),
            harness_root=self.root,
        )
        base = PreflightResult(
            arguments,
            empty_test_policy(),
            permission_report(self.root),
            "1.4.164",
            "a" * 40,
        )
        return prepare_agent_runtime(
            base,
            interactive=False,
            stderr=io.StringIO(),
            catalog=load_catalog(self.root, home=self.root),
        )

    def build(self, *extra: str):
        preflight = self.preflight(*extra)
        copy_path, digest = copy_request(self.control, self.request)
        return build_manifest(
            preflight,
            request_copy=copy_path,
            request_digest=digest,
            coordinator_handle="term-coordinator",
            pool=sample_pool(),
        )


class RoundTripTest(ManifestTestCase):
    def test_manifest_round_trip(self) -> None:
        manifest = self.build()
        write_manifest(self.control, manifest)
        loaded = read_manifest(self.control)
        self.assertEqual(manifest, loaded)
        assert loaded is not None
        self.assertIs(
            ConsensusProviderPolicy.DIVERSE,
            loaded.consensus_provider_policy,
        )
        self.assertIs(
            ConsensusIndependence.FULL,
            loaded.consensus_independence,
        )

    def test_missing_manifest_returns_none(self) -> None:
        self.assertIsNone(read_manifest(self.control))

    def test_malformed_manifest_raises(self) -> None:
        (self.control / "run-manifest.json").write_text(
            "{ not json",
            encoding="utf-8",
        )
        with self.assertRaises(ManifestError):
            read_manifest(self.control)

    def test_unknown_schema_version_is_rejected(self) -> None:
        manifest = self.build()
        raw = serialize_manifest(manifest).decode("utf-8")
        with self.assertRaises(ManifestError):
            parse_manifest(raw.replace('"schema_version": 3', '"schema_version": 9'))

    def test_legacy_version_one_migrates_without_a_run_id(self) -> None:
        # A run created before the Orca Run ID was persisted must stay
        # readable, so its status and reports keep working; resume is what
        # refuses it rather than guessing at a Run.
        manifest = self.build()
        raw = serialize_manifest(manifest).decode("utf-8")
        legacy = json.loads(raw)
        legacy["schema_version"] = 1
        legacy.pop("orchestration_run_id", None)
        migrated = parse_manifest(json.dumps(legacy))
        self.assertEqual(3, migrated.schema_version)

    def test_legacy_same_provider_migrates_as_degraded(self) -> None:
        manifest = self.build()
        legacy = json.loads(serialize_manifest(manifest).decode("utf-8"))
        legacy["schema_version"] = 2
        legacy.pop("consensus_provider_policy")
        legacy.pop("consensus_independence")
        legacy["agents"]["claude_code_review"]["provider"] = "codex"
        migrated = parse_manifest(json.dumps(legacy))
        self.assertIs(
            ConsensusProviderPolicy.LEGACY_UNSPECIFIED,
            migrated.consensus_provider_policy,
        )
        self.assertIs(
            ConsensusIndependence.DEGRADED,
            migrated.consensus_independence,
        )
        self.assertIsNone(migrated.orchestration_run_id)

    def test_orchestration_run_id_round_trips(self) -> None:
        manifest = replace(self.build(), orchestration_run_id="run_abc")
        reread = parse_manifest(serialize_manifest(manifest).decode("utf-8"))
        self.assertEqual("run_abc", reread.orchestration_run_id)

    def test_empty_orchestration_run_id_is_rejected(self) -> None:
        raw = serialize_manifest(self.build()).decode("utf-8")
        broken = json.loads(raw)
        broken["orchestration_run_id"] = ""
        with self.assertRaises(ManifestError):
            parse_manifest(json.dumps(broken))

    def test_non_ascii_request_is_copied_verbatim(self) -> None:
        manifest = self.build()
        copy_path = Path(manifest.request_copy)
        self.assertEqual(
            self.request.read_bytes(),
            copy_path.read_bytes(),
        )

    def test_records_requested_and_resolved_agent_values(self) -> None:
        manifest = self.build(
            "--agent-model",
            "claude_planner=sonnet5",
            "--agent-effort",
            "claude_planner=mid",
        )
        planner = {
            record.worker_key: record for record in manifest.agents
        }[WorkerKey.CLAUDE_PLANNER]
        self.assertEqual("sonnet5", planner.requested_model)
        self.assertEqual("sonnet", planner.model)
        self.assertEqual("alias", planner.model_method)
        self.assertEqual("medium", planner.effort)
        self.assertIs(AgentProvider.CLAUDE, planner.provider)


class VerifyInputsTest(ManifestTestCase):
    def test_clean_manifest_has_no_problems(self) -> None:
        self.assertEqual((), verify_inputs(self.build()))

    def test_deleted_original_request_is_not_a_problem(self) -> None:
        manifest = self.build()
        self.request.unlink()
        self.assertEqual((), verify_inputs(manifest))

    def test_missing_request_copy_is_reported(self) -> None:
        manifest = self.build()
        Path(manifest.request_copy).unlink()
        problems = verify_inputs(manifest)
        self.assertTrue(any("request copy" in item for item in problems))

    def test_changed_permission_report_is_reported(self) -> None:
        manifest = self.build()
        self.report.write_text('{"tampered": true}', encoding="utf-8")
        problems = verify_inputs(manifest)
        self.assertTrue(
            any("permission report content" in item for item in problems)
        )

    def test_missing_test_policy_is_reported(self) -> None:
        manifest = self.build()
        self.policy.unlink()
        problems = verify_inputs(manifest)
        self.assertTrue(any("test policy" in item for item in problems))


class RestoreArgumentsTest(ManifestTestCase):
    def test_arguments_are_restored_from_manifest(self) -> None:
        manifest = self.build()
        arguments = manifest_to_arguments(
            manifest,
            harness_root=self.root,
        )
        self.assertEqual("run-1", arguments.run_id)
        self.assertTrue(arguments.resume)
        self.assertFalse(arguments.dry_run)
        self.assertEqual(
            self.root,
            arguments.config.worktree_path,
        )
        self.assertEqual(
            Path(manifest.request_copy),
            arguments.config.request_path,
        )
        self.assertEqual(
            self.policy,
            arguments.config.test_policy_path,
        )
        self.assertEqual(
            "term-coordinator",
            arguments.config.coordinator_handle,
        )

    def test_resume_works_after_original_request_is_deleted(self) -> None:
        manifest = self.build()
        self.request.unlink()
        arguments = manifest_to_arguments(
            manifest,
            harness_root=self.root,
        )
        self.assertTrue(arguments.config.request_path.is_file())
        self.assertEqual(
            "작업 프롬프트 본문\n",
            arguments.config.request_path.read_text(encoding="utf-8"),
        )

    def test_tampered_permission_report_blocks_restore(self) -> None:
        manifest = self.build()
        self.report.write_text('{"tampered": true}', encoding="utf-8")
        with self.assertRaises(ManifestError):
            manifest_to_arguments(manifest, harness_root=self.root)

    def test_timeouts_survive_restore(self) -> None:
        manifest = self.build(
            "--step-timeout-ms",
            "3600000",
            "--total-timeout-ms",
            "14400000",
        )
        arguments = manifest_to_arguments(
            manifest,
            harness_root=self.root,
        )
        self.assertEqual(3_600_000, arguments.config.step_timeout_ms)
        self.assertEqual(14_400_000, arguments.config.total_timeout_ms)


class TerminalRecordTest(ManifestTestCase):
    def test_worker_pool_round_trip(self) -> None:
        manifest = self.build()
        pool = worker_pool_from_manifest(manifest)
        assert pool is not None
        self.assertEqual(4, len(pool.workers))
        self.assertEqual(
            {f"term-{worker.value}" for worker in WorkerKey},
            {item.terminal_handle for item in pool.workers},
        )

    def test_incomplete_pool_returns_none(self) -> None:
        manifest = self.build()
        trimmed = parse_manifest(
            serialize_manifest(manifest)
            .decode("utf-8")
            .replace('"codex_review": "term-codex_review"', '"unused": "x"')
        )
        self.assertIsNone(worker_pool_from_manifest(trimmed))

    def test_update_terminals_rewrites_manifest(self) -> None:
        manifest = self.build()
        write_manifest(self.control, manifest)
        rebound = WorkerPool(
            tuple(
                WorkerHandle(
                    worker_key=item.worker_key,
                    terminal_handle=f"new-{item.worker_key.value}",
                    worktree_id=item.worktree_id,
                    tab_id="",
                    leaf_id="",
                )
                for item in sample_pool().workers
            )
        )
        updated = update_terminals(
            self.control,
            manifest,
            coordinator_handle="term-new-coordinator",
            pool=rebound,
        )
        reloaded = read_manifest(self.control)
        self.assertEqual(updated, reloaded)
        assert reloaded is not None
        self.assertEqual(
            "term-new-coordinator",
            reloaded.coordinator_handle,
        )
        self.assertEqual(
            "new-claude_planner",
            reloaded.worker_handles()[WorkerKey.CLAUDE_PLANNER],
        )


class RequestCopyTest(ManifestTestCase):
    def test_second_copy_of_same_request_is_accepted(self) -> None:
        first, digest_one = copy_request(self.control, self.request)
        second, digest_two = copy_request(self.control, self.request)
        self.assertEqual(first, second)
        self.assertEqual(digest_one, digest_two)

    def test_changed_request_is_rejected(self) -> None:
        copy_request(self.control, self.request)
        self.request.write_text("다른 요청\n", encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "differs"):
            copy_request(self.control, self.request)


if __name__ == "__main__":
    unittest.main()
