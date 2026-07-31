from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence, TextIO

from .contracts import (
    ContractViolationError,
    build_agent_runtime_config,
    build_agent_runtime_snapshot,
    default_agent_provider,
    digest_value,
    parse_agent_runtime_config,
    parse_agent_runtime_snapshot,
    parse_permission_report,
    parse_test_policy,
    permission_capabilities,
    serialize_agent_runtime_config,
    serialize_agent_runtime_snapshot,
)
from .generation import AtomicWriteError, write_atomic_bytes
from .models import (
    AgentAccessMode,
    AgentProvider,
    AgentRuntimeConfig,
    AgentRuntimeOptions,
    LoopConfig,
    PermissionFeasibilityReport,
    PermissionStrategy,
    TestExecutionPolicy,
    ValidationStatus,
    WorkerKey,
)
from .orca_client import OrcaClient
from .workspace import PathBoundaryError, agent_runtime_snapshot_path


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
    agent_runtime_request: AgentRuntimeRequest | None = None


@dataclass(frozen=True)
class PreflightResult:
    arguments: RunArguments
    test_policy: TestExecutionPolicy
    permission_report: PermissionFeasibilityReport
    orca_version: str
    base_head: str
    agent_runtime: AgentRuntimeConfig | None = None


@dataclass(frozen=True)
class AgentRuntimeRequest:
    source_path: Path | None
    source_digest: str | None
    source_existed: bool
    base_config: AgentRuntimeConfig
    configure_agents: bool
    provider_overrides: tuple[tuple[WorkerKey, AgentProvider], ...]
    model_overrides: tuple[tuple[WorkerKey, str | None], ...]
    effort_overrides: tuple[tuple[WorkerKey, str | None], ...]
    explicit_input: bool


@dataclass(frozen=True)
class AgentRuntimeResolution:
    config: AgentRuntimeConfig
    source_config: AgentRuntimeConfig | None
    source_path: Path | None
    source_digest: str | None
    source_existed: bool
    write_source: bool


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


def default_agent_runtime_config() -> AgentRuntimeConfig:
    return build_agent_runtime_config(
        tuple(
            AgentRuntimeOptions(
                worker,
                default_agent_provider(worker),
                None,
                None,
            )
            for worker in WorkerKey
        )
    )


def _parse_agent_assignments(
    values: Sequence[str],
    option_name: str,
    *,
    allow_inherit: bool,
) -> tuple[tuple[WorkerKey, str | None], ...]:
    parsed: dict[WorkerKey, str | None] = {}
    for assignment in values:
        key, separator, value = assignment.partition("=")
        if not separator:
            raise ConfigurationError(
                f"{option_name} must use WORKER_KEY=VALUE"
            )
        key = key.strip()
        value = value.strip()
        try:
            worker = WorkerKey(key)
        except ValueError as exc:
            raise ConfigurationError(
                f"{option_name} has unknown worker: {key!r}"
            ) from exc
        if not value:
            raise ConfigurationError(
                f"{option_name} value must be nonempty"
            )
        if worker in parsed:
            raise ConfigurationError(
                f"{option_name} duplicates worker {worker.value}"
            )
        parsed[worker] = (
            None if allow_inherit and value.lower() == "inherit" else value
        )
    return tuple(
        (worker, parsed[worker])
        for worker in WorkerKey
        if worker in parsed
    )


def _parse_provider_assignments(
    values: Sequence[str],
) -> tuple[tuple[WorkerKey, AgentProvider], ...]:
    assignments = _parse_agent_assignments(
        values,
        "--agent-provider",
        allow_inherit=False,
    )
    parsed: list[tuple[WorkerKey, AgentProvider]] = []
    for worker, value in assignments:
        try:
            provider = AgentProvider(value)
        except ValueError as exc:
            raise ConfigurationError(
                "--agent-provider value must be claude or codex"
            ) from exc
        parsed.append((worker, provider))
    return tuple(parsed)


def _read_agent_runtime_request(
    source_argument: str | None,
    configure_agents: bool,
    provider_values: Sequence[str],
    model_values: Sequence[str],
    effort_values: Sequence[str],
) -> AgentRuntimeRequest:
    source_path: Path | None = None
    source_digest: str | None = None
    source_existed = False
    base_config = default_agent_runtime_config()
    if source_argument is not None:
        candidate = Path(source_argument).absolute()
        if candidate.is_symlink():
            raise ConfigurationError("agent config path must not be a symlink")
        source_path = candidate.resolve()
        if source_path.exists():
            if not source_path.is_file():
                raise ConfigurationError(
                    f"agent config must be a regular file: {source_path}"
                )
            try:
                raw = source_path.read_bytes()
                raw_text = raw.decode("utf-8")
                base_config = parse_agent_runtime_config(raw_text)
            except (OSError, UnicodeDecodeError, ContractViolationError) as exc:
                raise ConfigurationError(
                    f"invalid agent config {source_path}: {exc}"
                ) from exc
            source_existed = True
            source_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        elif not configure_agents:
            raise ConfigurationError(
                f"agent config does not exist: {source_path}"
            )
    if configure_agents and source_path is None:
        raise ConfigurationError(
            "--configure-agents requires --agent-config"
        )
    provider_overrides = _parse_provider_assignments(provider_values)
    model_overrides = _parse_agent_assignments(
        model_values,
        "--agent-model",
        allow_inherit=True,
    )
    effort_overrides = _parse_agent_assignments(
        effort_values,
        "--agent-effort",
        allow_inherit=True,
    )
    return AgentRuntimeRequest(
        source_path=source_path,
        source_digest=source_digest,
        source_existed=source_existed,
        base_config=base_config,
        configure_agents=configure_agents,
        provider_overrides=provider_overrides,
        model_overrides=model_overrides,
        effort_overrides=effort_overrides,
        explicit_input=bool(
            source_path
            or configure_agents
            or provider_overrides
            or model_overrides
            or effort_overrides
        ),
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _runtime_mapping(
    config: AgentRuntimeConfig,
) -> dict[WorkerKey, AgentRuntimeOptions]:
    return {item.worker_key: item for item in config.agents}


def _read_interactive_value(
    worker: WorkerKey,
    field: str,
    current: str | None,
    input_fn: Callable[[], str],
    stderr: TextIO,
) -> str | None:
    display = current if current is not None else "<provider-default>"
    print(
        f"[{worker.value}] {field} [current: {display}]: ",
        end="",
        file=stderr,
        flush=True,
    )
    answer = input_fn().strip()
    if not answer:
        return current
    if answer.lower() == "inherit":
        return None
    return answer


def _read_interactive_provider(
    worker: WorkerKey,
    current: AgentProvider,
    input_fn: Callable[[], str],
    stderr: TextIO,
) -> AgentProvider:
    print(
        f"[{worker.value}] provider [current: {current.value}]: ",
        end="",
        file=stderr,
        flush=True,
    )
    answer = input_fn().strip().lower()
    if not answer:
        return current
    try:
        return AgentProvider(answer)
    except ValueError as exc:
        raise ConfigurationError(
            "agent provider must be claude or codex"
        ) from exc


def resolve_agent_runtime(
    request: AgentRuntimeRequest | None,
    *,
    resume: bool,
    worktree_path: Path,
    interactive: bool | None = None,
    input_fn: Callable[[], str] | None = None,
    stderr: TextIO | None = None,
) -> AgentRuntimeResolution:
    current_request = request or AgentRuntimeRequest(
        source_path=None,
        source_digest=None,
        source_existed=False,
        base_config=default_agent_runtime_config(),
        configure_agents=False,
        provider_overrides=(),
        model_overrides=(),
        effort_overrides=(),
        explicit_input=False,
    )
    if current_request.configure_agents and resume:
        raise ConfigurationError(
            "--configure-agents cannot be used with --resume"
        )
    if (
        current_request.configure_agents
        and current_request.source_path is not None
        and _inside(current_request.source_path, worktree_path)
    ):
        raise ConfigurationError(
            "interactive agent config must be outside the target worktree"
        )
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    reader = input if input_fn is None else input_fn
    output = sys.stderr if stderr is None else stderr
    mapping = _runtime_mapping(current_request.base_config)
    write_source = False
    source_config: AgentRuntimeConfig | None = None
    try:
        if current_request.configure_agents:
            if not is_interactive:
                raise ConfigurationError(
                    "--configure-agents requires an interactive terminal"
                )
            updated: dict[WorkerKey, AgentRuntimeOptions] = {}
            for worker in WorkerKey:
                existing = mapping[worker]
                provider = _read_interactive_provider(
                    worker,
                    existing.provider,
                    reader,
                    output,
                )
                provider_changed = provider is not existing.provider
                model = _read_interactive_value(
                    worker,
                    "model",
                    None if provider_changed else existing.model,
                    reader,
                    output,
                )
                effort = _read_interactive_value(
                    worker,
                    "effort",
                    None if provider_changed else existing.effort,
                    reader,
                    output,
                )
                updated[worker] = AgentRuntimeOptions(
                    worker,
                    provider,
                    model,
                    effort,
                )
            mapping = updated
            candidate = build_agent_runtime_config(
                tuple(mapping[worker] for worker in WorkerKey)
            )
            source_config = candidate
            print_agent_runtime_summary(candidate, stderr=output)
            print(
                "Save agent configuration? [y/N]: ",
                end="",
                file=output,
                flush=True,
            )
            if reader().strip().lower() != "y":
                raise ConfigurationError(
                    "agent runtime configuration cancelled"
                )
            write_source = True
    except (EOFError, KeyboardInterrupt) as exc:
        raise ConfigurationError(
            "agent runtime configuration cancelled"
        ) from exc

    provider_overrides = dict(current_request.provider_overrides)
    model_overrides = dict(current_request.model_overrides)
    effort_overrides = dict(current_request.effort_overrides)
    for worker, provider in provider_overrides.items():
        existing = mapping[worker]
        if provider is not existing.provider:
            stale_fields = tuple(
                field
                for field, value, overrides in (
                    ("model", existing.model, model_overrides),
                    ("effort", existing.effort, effort_overrides),
                )
                if value is not None and worker not in overrides
            )
            if stale_fields:
                joined = ", ".join(stale_fields)
                raise ConfigurationError(
                    f"--agent-provider changes {worker.value} but leaves "
                    f"provider-specific {joined}; explicitly override each "
                    "value or set it to inherit"
                )
        mapping[worker] = AgentRuntimeOptions(
            worker,
            provider,
            existing.model,
            existing.effort,
        )
    resolved = tuple(
        AgentRuntimeOptions(
            worker,
            mapping[worker].provider,
            (
                model_overrides[worker]
                if worker in model_overrides
                else mapping[worker].model
            ),
            (
                effort_overrides[worker]
                if worker in effort_overrides
                else mapping[worker].effort
            ),
        )
        for worker in WorkerKey
    )
    try:
        config = build_agent_runtime_config(resolved)
    except ContractViolationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return AgentRuntimeResolution(
        config=config,
        source_config=source_config,
        source_path=current_request.source_path,
        source_digest=current_request.source_digest,
        source_existed=current_request.source_existed,
        write_source=write_source,
    )


def print_agent_runtime_summary(
    config: AgentRuntimeConfig,
    *,
    stderr: TextIO | None = None,
) -> None:
    output = sys.stderr if stderr is None else stderr
    print("Resolved agent runtime:", file=output)
    for item in config.agents:
        model = item.model or "<provider-default>"
        effort = item.effort or "<provider-default>"
        print(
            f"- {item.worker_key.value}: provider={item.provider.value}, "
            f"model={model}, effort={effort}",
            file=output,
        )


def persist_agent_runtime_source(
    resolution: AgentRuntimeResolution,
) -> Path | None:
    if not resolution.write_source:
        return None
    if resolution.source_path is None or resolution.source_config is None:
        raise ConfigurationError(
            "confirmed agent configuration has no source target"
        )
    target = resolution.source_path
    if target.is_symlink():
        raise ConfigurationError("agent config path must not be a symlink")
    if not target.parent.is_dir():
        raise ConfigurationError(
            f"agent config parent does not exist: {target.parent}"
        )
    if resolution.source_existed:
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise ConfigurationError(
                f"failed to reread agent config: {target}"
            ) from exc
        current_digest = "sha256:" + hashlib.sha256(current).hexdigest()
        if current_digest != resolution.source_digest:
            raise ConfigurationError(
                "agent config changed during interactive configuration"
            )
    elif target.exists():
        raise ConfigurationError(
            "agent config was created during interactive configuration"
        )
    raw = (
        serialize_agent_runtime_config(resolution.source_config).encode("utf-8")
        + b"\n"
    )
    try:
        return write_atomic_bytes(target, raw)
    except (AtomicWriteError, ContractViolationError) as exc:
        raise ConfigurationError(str(exc)) from exc


def load_agent_runtime_snapshot(
    harness_root: Path,
    run_id: str,
) -> AgentRuntimeConfig | None:
    try:
        path = agent_runtime_snapshot_path(harness_root, run_id)
    except PathBoundaryError as exc:
        raise PreflightError(str(exc)) from exc
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PreflightError(
            f"agent runtime snapshot must be a regular file: {path}"
        )
    try:
        snapshot = parse_agent_runtime_snapshot(
            path.read_text(encoding="utf-8"),
            run_id,
        )
    except (OSError, UnicodeDecodeError, ContractViolationError) as exc:
        raise PreflightError(
            f"invalid agent runtime snapshot: {exc}"
        ) from exc
    return build_agent_runtime_config(snapshot.agents)


def persist_agent_runtime_snapshot(
    control_dir: Path,
    run_id: str,
    config: AgentRuntimeConfig,
    source_path: Path | None,
) -> Path:
    target = control_dir.resolve() / "agent-runtime.json"
    snapshot = build_agent_runtime_snapshot(
        run_id,
        config,
        None if source_path is None else str(source_path.resolve()),
    )
    raw = serialize_agent_runtime_snapshot(snapshot).encode("utf-8") + b"\n"
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise AtomicWriteError(
                f"agent runtime snapshot must be a regular file: {target}"
            )
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise AtomicWriteError(
                f"failed to read agent runtime snapshot: {target}"
            ) from exc
        if existing != raw:
            raise AtomicWriteError(
                "agent runtime snapshot already exists with different content"
            )
        return target
    return write_atomic_bytes(target, raw)


def prepare_agent_runtime(
    preflight: PreflightResult,
    *,
    interactive: bool | None = None,
    input_fn: Callable[[], str] | None = None,
    stderr: TextIO | None = None,
) -> PreflightResult:
    arguments = preflight.arguments
    request = arguments.agent_runtime_request
    persisted = (
        load_agent_runtime_snapshot(
            arguments.harness_root,
            arguments.run_id,
        )
        if arguments.resume
        else None
    )
    if persisted is not None:
        if request is not None and request.configure_agents:
            raise ConfigurationError(
                "--configure-agents cannot be used with --resume"
            )
        if request is not None and request.explicit_input:
            candidate = resolve_agent_runtime(
                request,
                resume=True,
                worktree_path=arguments.config.worktree_path,
                interactive=interactive,
                input_fn=input_fn,
                stderr=stderr,
            ).config
            if candidate.configuration_digest != persisted.configuration_digest:
                raise PreflightError(
                    "agent runtime configuration drift on resume"
                )
        resolved = persisted
    else:
        resolution = resolve_agent_runtime(
            request,
            resume=arguments.resume,
            worktree_path=arguments.config.worktree_path,
            interactive=interactive,
            input_fn=input_fn,
            stderr=stderr,
        )
        persist_agent_runtime_source(resolution)
        resolved = resolution.config
        if arguments.resume:
            output = sys.stderr if stderr is None else stderr
            print(
                "Legacy run has no agent runtime snapshot; "
                "a migration snapshot will be created.",
                file=output,
            )
    capabilities = permission_capabilities(preflight.permission_report)
    for item in resolved.agents:
        access_mode = (
            AgentAccessMode.WRITABLE
            if item.worker_key is WorkerKey.CODEX_IMPLEMENTER
            else AgentAccessMode.READ_ONLY
        )
        if not any(
            capability.provider is item.provider
            and capability.access_mode is access_mode
            for capability in capabilities
        ):
            check_hint = (
                "V-PERM-06"
                if item.provider is AgentProvider.CLAUDE
                and access_mode is AgentAccessMode.WRITABLE
                else "the matching permission check"
            )
            raise PreflightError(
                f"permission report does not prove {item.provider.value} "
                f"{access_mode.value} capability required by "
                f"{item.worker_key.value}; pass {check_hint} before "
                "worker provisioning"
            )
    print_agent_runtime_summary(resolved, stderr=stderr)
    return replace(preflight, agent_runtime=resolved)


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
        "--agent-config",
        metavar="PATH",
        help="Load the strict JSON configuration for all four agents.",
    )
    parser.add_argument(
        "--configure-agents",
        action="store_true",
        help="Interactively update --agent-config before the run.",
    )
    parser.add_argument(
        "--agent-provider",
        action="append",
        default=[],
        metavar="WORKER_KEY=PROVIDER",
        help=(
            "Override one agent provider (claude or codex) for this run; "
            "may be repeated."
        ),
    )
    parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="WORKER_KEY=MODEL",
        help="Override one agent model for this run; may be repeated.",
    )
    parser.add_argument(
        "--agent-effort",
        action="append",
        default=[],
        metavar="WORKER_KEY=EFFORT",
        help="Override one agent effort for this run; may be repeated.",
    )
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
    agent_runtime_request = _read_agent_runtime_request(
        namespace.agent_config,
        namespace.configure_agents,
        namespace.agent_provider,
        namespace.agent_model,
        namespace.agent_effort,
    )
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
        agent_runtime_request=agent_runtime_request,
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
