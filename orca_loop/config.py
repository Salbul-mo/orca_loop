from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .contracts import (
    ContractViolationError,
    digest_value,
    parse_permission_report,
    parse_test_policy,
)
from .models import (
    LoopConfig,
    PermissionFeasibilityReport,
    PermissionStrategy,
    TestExecutionPolicy,
    ValidationStatus,
)
from .orca_client import OrcaClient


DEFAULT_PLAN_ROUND_LIMIT = 5
DEFAULT_CODE_ROUND_LIMIT = 5
DEFAULT_TEST_FIX_ATTEMPT_LIMIT = 3
DEFAULT_OPERATIONAL_RETRY_LIMIT = 3
DEFAULT_MAX_TRANSITION_COUNT = 128
DEFAULT_STEP_TIMEOUT_MS = 900_000
DEFAULT_TOTAL_TIMEOUT_MS = 7_200_000


class ConfigurationError(ValueError):
    """Raised when deterministic loop configuration is invalid."""


class PreflightError(ConfigurationError):
    """Raised when a read-only preflight requirement is not satisfied."""


@dataclass(frozen=True)
class RunArguments:
    run_id: str
    harness_root: Path
    config: LoopConfig
    permission_report_path: Path
    resume: bool
    dry_run: bool


@dataclass(frozen=True)
class PreflightResult:
    arguments: RunArguments
    test_policy: TestExecutionPolicy
    permission_report: PermissionFeasibilityReport
    orca_version: str
    base_head: str


def empty_test_policy() -> TestExecutionPolicy:
    value = {
        "allowed_commands": [],
        "allowed_env_keys": [],
        "allowed_output_paths": [],
        "approved_kinds": [],
    }
    return TestExecutionPolicy(
        allowed_commands=(),
        allowed_env_keys=(),
        allowed_output_paths=(),
        approved_kinds=(),
        policy_digest=digest_value(value),
    )


def load_test_policy(path: Path | None) -> TestExecutionPolicy:
    if path is None:
        return empty_test_policy()
    resolved = path.resolve()
    if not resolved.is_file():
        raise ConfigurationError(
            f"test policy does not exist: {resolved}"
        )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            "test policy must be UTF-8"
        ) from exc
    return parse_test_policy(raw)


def validate_loop_config(config: LoopConfig) -> LoopConfig:
    if not config.worktree_path.is_absolute():
        raise ConfigurationError("worktree_path must be absolute")
    if not config.request_path.is_absolute():
        raise ConfigurationError("request_path must be absolute")
    if not config.coordinator_handle:
        raise ConfigurationError("coordinator_handle must be nonempty")
    if config.plan_consensus_round_limit != 5:
        raise ConfigurationError(
            "plan_consensus_round_limit must remain the approved value 5"
        )
    if config.code_consensus_round_limit != 5:
        raise ConfigurationError(
            "code_consensus_round_limit must remain the approved value 5"
        )
    numeric = (
        config.test_fix_attempt_limit,
        config.operational_retry_limit,
        config.max_transition_count,
        config.step_timeout_ms,
        config.total_timeout_ms,
    )
    if any(value < 1 for value in numeric):
        raise ConfigurationError("loop limits and timeouts must be positive")
    if config.step_timeout_ms > config.total_timeout_ms:
        raise ConfigurationError(
            "step_timeout_ms cannot exceed total_timeout_ms"
        )
    return config


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic Orca staged-development loop."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--coordinator-handle", required=True)
    parser.add_argument("--permission-report", required=True)
    parser.add_argument("--test-policy")
    parser.add_argument(
        "--plan-consensus-round-limit",
        type=int,
        default=DEFAULT_PLAN_ROUND_LIMIT,
        choices=(5,),
    )
    parser.add_argument(
        "--code-consensus-round-limit",
        type=int,
        default=DEFAULT_CODE_ROUND_LIMIT,
        choices=(5,),
    )
    parser.add_argument(
        "--test-fix-attempt-limit",
        type=int,
        default=DEFAULT_TEST_FIX_ATTEMPT_LIMIT,
    )
    parser.add_argument(
        "--operational-retry-limit",
        type=int,
        default=DEFAULT_OPERATIONAL_RETRY_LIMIT,
    )
    parser.add_argument(
        "--max-transition-count",
        type=int,
        default=DEFAULT_MAX_TRANSITION_COUNT,
    )
    parser.add_argument(
        "--step-timeout-ms",
        type=int,
        default=DEFAULT_STEP_TIMEOUT_MS,
    )
    parser.add_argument(
        "--total-timeout-ms",
        type=int,
        default=DEFAULT_TOTAL_TIMEOUT_MS,
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def parse_run_arguments(
    argv: Sequence[str] | None,
    *,
    harness_root: Path,
) -> RunArguments:
    namespace = build_argument_parser().parse_args(argv)
    root = harness_root.resolve()
    request = Path(namespace.request).resolve()
    worktree = Path(namespace.worktree).resolve()
    policy = (
        None
        if namespace.test_policy is None
        else Path(namespace.test_policy).resolve()
    )
    permission = Path(namespace.permission_report).resolve()
    config = validate_loop_config(
        LoopConfig(
            worktree_path=worktree,
            request_path=request,
            coordinator_handle=namespace.coordinator_handle,
            test_policy_path=policy,
            plan_consensus_round_limit=(
                namespace.plan_consensus_round_limit
            ),
            code_consensus_round_limit=(
                namespace.code_consensus_round_limit
            ),
            test_fix_attempt_limit=namespace.test_fix_attempt_limit,
            operational_retry_limit=namespace.operational_retry_limit,
            max_transition_count=namespace.max_transition_count,
            step_timeout_ms=namespace.step_timeout_ms,
            total_timeout_ms=namespace.total_timeout_ms,
        )
    )
    return RunArguments(
        run_id=namespace.run_id,
        harness_root=root,
        config=config,
        permission_report_path=permission,
        resume=namespace.resume,
        dry_run=namespace.dry_run,
    )


def _git(worktree: Path, *argv: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(worktree), *argv),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PreflightError(
            f"Git preflight failed for {argv!r}: {result.stderr[-4096:]}"
        )
    return result.stdout


def _orca_version(status: dict[str, object]) -> str:
    for key in ("appVersion", "app_version", "version"):
        value = status.get(key)
        if isinstance(value, str) and value:
            return value
    for nested_key in ("runtime", "app"):
        nested = status.get(nested_key)
        if isinstance(nested, dict):
            try:
                return _orca_version(nested)
            except PreflightError:
                continue
    raise PreflightError("Orca status has no app version")


def run_preflight(
    arguments: RunArguments,
    client: OrcaClient,
    *,
    expected_orca_version: str = "1.4.159",
) -> PreflightResult:
    config = arguments.config
    if sys.version_info < (3, 11):
        raise PreflightError("Python 3.11 or newer is required")
    if not arguments.harness_root.is_dir():
        raise PreflightError(
            f"harness root does not exist: {arguments.harness_root}"
        )
    if not config.worktree_path.is_dir():
        raise PreflightError(
            f"worktree does not exist: {config.worktree_path}"
        )
    if not config.request_path.is_file():
        raise PreflightError(
            f"request file does not exist: {config.request_path}"
        )
    try:
        config.request_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PreflightError("request file must be UTF-8") from exc
    base_head = _git(config.worktree_path, "rev-parse", "HEAD").strip()
    if not base_head:
        raise PreflightError("target worktree has no HEAD")
    dirty = _git(config.worktree_path, "status", "--porcelain")
    if dirty and not arguments.resume:
        raise PreflightError(
            "target worktree must be clean before a new run"
        )
    if not arguments.permission_report_path.is_file():
        raise PreflightError(
            "permission feasibility report does not exist"
        )
    try:
        permission = parse_permission_report(
            arguments.permission_report_path.read_text(encoding="utf-8")
        )
        policy = load_test_policy(config.test_policy_path)
    except (ContractViolationError, ConfigurationError) as exc:
        raise PreflightError(str(exc)) from exc
    status_response = client.call(("status",), timeout_ms=10_000)
    try:
        status = json.loads(status_response.result_json)
    except json.JSONDecodeError as exc:
        raise PreflightError("Orca status result is malformed") from exc
    if not isinstance(status, dict):
        raise PreflightError("Orca status result must be an object")
    runtime = status.get("runtime")
    graph = status.get("graph")
    if (
        not isinstance(runtime, dict)
        or runtime.get("state") != "ready"
        or runtime.get("reachable") is not True
        or not isinstance(graph, dict)
        or graph.get("state") != "ready"
    ):
        raise PreflightError("Orca runtime and graph must both be ready")
    version = _orca_version(status)
    if version != expected_orca_version:
        raise PreflightError(
            f"Orca version drift: expected {expected_orca_version}, got {version}"
        )
    if (
        permission.status is not ValidationStatus.PASS
        or permission.strategy
        is not PermissionStrategy.READONLY_REPOSITORY
        or permission.orca_version != version
    ):
        raise PreflightError(
            "permission report is not a PASS result for strategy D "
            "and the active Orca version"
        )
    if (
        Path(permission.canonical_path).resolve()
        != arguments.permission_report_path
    ):
        raise PreflightError(
            "permission report path differs from canonical_path"
        )
    if any(
        item.status is not ValidationStatus.PASS
        for item in permission.checks
    ):
        raise PreflightError(
            "permission report contains a non-PASS check"
        )
    client.call(
        (
            "terminal",
            "show",
            "--terminal",
            config.coordinator_handle,
        ),
        timeout_ms=10_000,
    )
    return PreflightResult(
        arguments=arguments,
        test_policy=policy,
        permission_report=permission,
        orca_version=version,
        base_head=base_head,
    )
