from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from .catalog import (
    AgentCatalog,
    ResolvedValue,
    UnknownAgentValueError,
    describe_value,
    load_catalog,
    resolve_agent,
)
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
from .environment import (
    capture_environment,
    compare_environment,
    environment_notes,
)
from .generation import AtomicWriteError, write_atomic_bytes
from .models import (
    AgentAccessMode,
    AgentProvider,
    AgentRuntimeConfig,
    AgentRuntimeOptions,
    DEFAULT_NOTICE_CHANNELS,
    LoopConfig,
    NoticeChannel,
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
PERMISSION_REFRESH_MARKER_NAME = ".permission-refresh-required.json"


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
    strict_agent_runtime: bool = False
    accept_worktree_drift: bool = False
    force_unlock: bool = False


@dataclass(frozen=True)
class AgentResolution:
    """Requested-to-resolved provenance for one worker slot."""

    worker_key: WorkerKey
    provider: AgentProvider
    model: ResolvedValue
    effort: ResolvedValue

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            item
            for item in (self.model.warning, self.effort.warning)
            if item
        )


@dataclass(frozen=True)
class PreflightResult:
    arguments: RunArguments
    test_policy: TestExecutionPolicy
    permission_report: PermissionFeasibilityReport
    orca_version: str
    base_head: str
    agent_runtime: AgentRuntimeConfig | None = None
    agent_resolutions: tuple[AgentResolution, ...] = ()


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


def normalize_agent_runtime(
    config: AgentRuntimeConfig,
    catalog: AgentCatalog,
    *,
    strict: bool = False,
) -> tuple[AgentRuntimeConfig, tuple[AgentResolution, ...]]:
    """Map requested model and effort values onto catalog values.

    The provider of each worker is preserved exactly: the permission
    feasibility report proves capabilities per provider, so resolution must
    never move a worker to a provider the report does not cover.
    """
    mapping = _runtime_mapping(config)
    options: list[AgentRuntimeOptions] = []
    resolutions: list[AgentResolution] = []
    for worker in WorkerKey:
        current = mapping[worker]
        try:
            resolved = resolve_agent(
                catalog,
                current.provider,
                current.model,
                current.effort,
                strict=strict,
            )
        except UnknownAgentValueError as exc:
            raise ConfigurationError(
                f"{worker.value}: {exc}"
            ) from exc
        options.append(
            AgentRuntimeOptions(
                worker,
                current.provider,
                resolved.model.value,
                resolved.effort.value,
            )
        )
        resolutions.append(
            AgentResolution(
                worker_key=worker,
                provider=current.provider,
                model=resolved.model,
                effort=resolved.effort,
            )
        )
    try:
        normalized = build_agent_runtime_config(tuple(options))
    except ContractViolationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return normalized, tuple(resolutions)


def print_agent_runtime_summary(
    config: AgentRuntimeConfig,
    *,
    stderr: TextIO | None = None,
    resolutions: tuple[AgentResolution, ...] = (),
) -> None:
    output = sys.stderr if stderr is None else stderr
    by_worker = {item.worker_key: item for item in resolutions}
    print("Resolved agent runtime:", file=output)
    for item in config.agents:
        resolution = by_worker.get(item.worker_key)
        if resolution is None:
            model = item.model or "<provider-default>"
            effort = item.effort or "<provider-default>"
        else:
            model = describe_value(resolution.model)
            effort = describe_value(resolution.effort)
        print(
            f"- {item.worker_key.value}: provider={item.provider.value}, "
            f"model={model}, effort={effort}",
            file=output,
        )
    for resolution in resolutions:
        for warning in resolution.warnings:
            print(
                f"[WARN] {resolution.worker_key.value}: {warning}",
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
    catalog: AgentCatalog | None = None,
) -> PreflightResult:
    arguments = preflight.arguments
    request = arguments.agent_runtime_request
    active_catalog = (
        load_catalog(arguments.harness_root)
        if catalog is None
        else catalog
    )
    strict = arguments.strict_agent_runtime
    resolutions: tuple[AgentResolution, ...] = ()
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
            # Normalize the candidate the same way the original start did,
            # so re-typing "sonnet5" for a run launched as "sonnet" is not
            # reported as configuration drift.
            candidate, _ = normalize_agent_runtime(
                candidate,
                active_catalog,
                strict=strict,
            )
            if candidate.configuration_digest != persisted.configuration_digest:
                raise PreflightError(
                    "agent runtime configuration drift on resume"
                )
        # The persisted snapshot is what the run has been using; it is never
        # re-normalized, because changing it mid-run is configuration drift.
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
        resolved, resolutions = normalize_agent_runtime(
            resolution.config,
            active_catalog,
            strict=strict,
        )
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
    print_agent_runtime_summary(
        resolved,
        stderr=stderr,
        resolutions=resolutions,
    )
    return replace(
        preflight,
        agent_runtime=resolved,
        agent_resolutions=resolutions,
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


NOTICE_CHANNEL_ALIASES: Mapping[str, NoticeChannel] = {
    "board": NoticeChannel.ORCA_BOARD,
    "file-open": NoticeChannel.ORCA_FILE_OPEN,
    "terminal-focus": NoticeChannel.ORCA_TERMINAL_FOCUS,
    "os-toast": NoticeChannel.OS_TOAST,
}


def parse_notice_channels(value: str | None) -> tuple[NoticeChannel, ...]:
    """Resolve the ``--notice-channels`` flag into an ordered channel tuple."""
    if value is None:
        return DEFAULT_NOTICE_CHANNELS
    normalized = value.strip().lower()
    if normalized == "none":
        return ()
    selected: list[NoticeChannel] = []
    for token in normalized.split(","):
        name = token.strip()
        if not name:
            raise ConfigurationError(
                "notice channel names must not be empty; "
                f"valid names are {sorted(NOTICE_CHANNEL_ALIASES)} or none"
            )
        channel = NOTICE_CHANNEL_ALIASES.get(name)
        if channel is None:
            raise ConfigurationError(
                f"unknown notice channel: {name}; "
                f"valid names are {sorted(NOTICE_CHANNEL_ALIASES)} or none"
            )
        if channel not in selected:
            selected.append(channel)
    return tuple(selected)


def validate_loop_config(config: LoopConfig) -> LoopConfig:
    if not config.worktree_path.is_absolute():
        raise ConfigurationError("worktree_path must be absolute")
    if not config.request_path.is_absolute():
        raise ConfigurationError("request_path must be absolute")
    if not config.coordinator_handle:
        raise ConfigurationError("coordinator_handle must be nonempty")
    if len(set(config.notice_channels)) != len(config.notice_channels):
        raise ConfigurationError("notice_channels must not repeat a channel")
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
        "--strict-agent-runtime",
        action="store_true",
        help=(
            "Reject model or effort values that need a tolerant fallback "
            "instead of resolving them to the closest catalog value."
        ),
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
    parser.add_argument(
        "--notice-channels",
        help=(
            "Comma-separated user decision notification channels: "
            f"{', '.join(sorted(NOTICE_CHANNEL_ALIASES))}, or none. "
            "Defaults to every channel."
        ),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--accept-worktree-drift",
        action="store_true",
        help=(
            "Resume a write step even though the worktree changed since the "
            "last committed generation."
        ),
    )
    parser.add_argument(
        "--force-unlock",
        action="store_true",
        help=(
            "Reclaim the coordinator lock even when its owning process still "
            "appears to be running."
        ),
    )
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
            notice_channels=parse_notice_channels(
                getattr(namespace, "notice_channels", None)
            ),
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
        strict_agent_runtime=getattr(
            namespace,
            "strict_agent_runtime",
            False,
        ),
        accept_worktree_drift=getattr(
            namespace,
            "accept_worktree_drift",
            False,
        ),
        force_unlock=getattr(namespace, "force_unlock", False),
    )


def permission_report_candidates(harness_root: Path) -> tuple[Path, ...]:
    runs_root = harness_root.resolve() / "runs"
    if not runs_root.is_dir():
        return ()
    found = [
        path
        for path in runs_root.glob("*/control/permission-feasibility.json")
        if path.is_file()
    ]
    return tuple(
        sorted(
            found,
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    )


def permission_environment_problems(
    report: PermissionFeasibilityReport,
    harness_root: Path,
    orca_version: str,
) -> tuple[str, ...]:
    """Return the blocking environment differences for a report.

    A report that records an environment fingerprint is judged on that
    fingerprint. Only a legacy report without one falls back to the old exact
    Orca version comparison, because it carries no better evidence.
    """
    if report.environment is None:
        if report.orca_version != orca_version:
            return (
                f"legacy report without environment fingerprint and "
                f"orca_version {report.orca_version} does not match "
                f"{orca_version}",
            )
        return ()
    return compare_environment(
        report.environment,
        capture_environment(harness_root),
    )


def permission_report_notes(
    report: PermissionFeasibilityReport,
    orca_version: str,
) -> tuple[str, ...]:
    """Return non-blocking observations about a usable report."""
    notes: list[str] = []
    if report.environment is not None:
        notes.extend(
            environment_notes(
                report.environment,
                capture_environment(Path(report.canonical_path).resolve().parents[3]),
            )
        )
    if report.environment is not None and report.orca_version != orca_version:
        notes.append(
            f"permission report was produced under Orca {report.orca_version} "
            f"and the runtime is {orca_version}; Orca does not mediate file "
            "access, so this is informational"
        )
    return tuple(notes)


def permission_refresh_marker_path(harness_root: Path) -> Path:
    return harness_root.resolve() / "runs" / PERMISSION_REFRESH_MARKER_NAME


def _parse_utc(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PreflightError(f"{context} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PreflightError(f"{context} must use UTC offset")
    return parsed


def _read_permission_refresh_marker(harness_root: Path) -> dict[str, object] | None:
    path = permission_refresh_marker_path(harness_root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"permission refresh marker is unreadable: {exc}") from exc
    required = {
        "schema_version", "run_id", "detected_at", "reason_code",
        "worker_key", "step_id", "blocked_report_digest", "evidence_paths",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PreflightError("permission refresh marker schema is invalid")
    if value.get("schema_version") != 1 or not isinstance(value["detected_at"], str):
        raise PreflightError("permission refresh marker schema is invalid")
    _parse_utc(value["detected_at"], "permission refresh marker detected_at")
    if not isinstance(value["blocked_report_digest"], str):
        raise PreflightError("permission refresh marker digest is invalid")
    return value


def _remove_marker_atomically(path: Path) -> None:
    retired = path.with_name(path.name + ".cleared")
    os.replace(path, retired)
    retired.unlink()


def permission_refresh_problem(
    harness_root: Path,
    report: PermissionFeasibilityReport,
) -> str | None:
    marker = _read_permission_refresh_marker(harness_root)
    if marker is None:
        return None
    if report.created_at is None:
        return "permission refresh required; selected report has no created_at"
    created_at = _parse_utc(report.created_at, "permission report created_at")
    detected_at = _parse_utc(str(marker["detected_at"]), "permission refresh marker detected_at")
    if (
        report.report_digest == marker["blocked_report_digest"]
        or created_at <= detected_at
    ):
        return "permission refresh required; selected report predates the recorded permission failure"
    try:
        _remove_marker_atomically(permission_refresh_marker_path(harness_root))
    except OSError as exc:
        return f"permission refresh marker could not be cleared: {exc}"
    return None


def record_permission_refresh_marker(
    harness_root: Path,
    *,
    run_id: str,
    reason_code: str,
    worker_key: str,
    step_id: str,
    blocked_report_digest: str,
    evidence_paths: Sequence[str],
) -> None:
    value = {
        "schema_version": 1,
        "run_id": run_id,
        "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason_code": reason_code,
        "worker_key": worker_key,
        "step_id": step_id,
        "blocked_report_digest": blocked_report_digest,
        "evidence_paths": list(evidence_paths),
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    write_atomic_bytes(permission_refresh_marker_path(harness_root), raw)


def permission_report_problem(
    path: Path,
    orca_version: str,
    *,
    harness_root: Path | None = None,
) -> str | None:
    """Return why a report is unusable, or None when it qualifies.

    These are exactly the conditions an operator was previously expected to
    check by hand before every launch.
    """
    try:
        report = parse_permission_report(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ContractViolationError) as exc:
        return f"unreadable ({exc})"
    if report.status is not ValidationStatus.PASS:
        return f"status is {report.status.value}"
    if report.strategy is not PermissionStrategy.READONLY_REPOSITORY:
        return f"strategy is {report.strategy.value}"
    environment_problems = permission_environment_problems(
        report,
        # <harness>/runs/<run-id>/control/permission-feasibility.json
        path.resolve().parents[3] if harness_root is None else harness_root,
        orca_version,
    )
    if environment_problems:
        return "; ".join(environment_problems)
    if any(item.status is not ValidationStatus.PASS for item in report.checks):
        failing = ", ".join(
            item.check_id
            for item in report.checks
            if item.status is not ValidationStatus.PASS
        )
        return f"non-PASS checks: {failing}"
    try:
        canonical = Path(report.canonical_path).resolve()
    except OSError:
        return "canonical_path cannot be resolved"
    if canonical != path.resolve():
        return f"canonical_path points elsewhere: {report.canonical_path}"
    refresh_problem = permission_refresh_problem(
        path.resolve().parents[3] if harness_root is None else harness_root,
        report,
    )
    if refresh_problem:
        return refresh_problem
    return None


def discover_permission_report(
    harness_root: Path,
    orca_version: str,
) -> Path:
    """Pick the newest permission report that satisfies every gate."""
    candidates = permission_report_candidates(harness_root)
    if not candidates:
        raise PreflightError(
            "no permission feasibility report found under "
            f"{harness_root / 'runs'}; run the permission spike first"
        )
    problems: list[str] = []
    for path in candidates:
        problem = permission_report_problem(
            path,
            orca_version,
            harness_root=harness_root,
        )
        if problem is None:
            return path
        problems.append(f"{path}: {problem}")
    raise PreflightError(
        "no permission feasibility report qualifies for Orca "
        f"{orca_version}: " + "; ".join(problems[:5])
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


def orca_version_from_status(status: dict[str, object]) -> str:
    """Read the Orca app version out of a `status --json` result."""
    return _orca_version(status)


def run_preflight(
    arguments: RunArguments,
    client: OrcaClient,
    *,
    expected_orca_version: str = "1.4.159",
    verify_coordinator: bool = True,
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
        # Informational only: Orca does not mediate file access, so a version
        # change does not by itself invalidate the permission proof.
        print(
            f"[NOTE] Orca runtime is {version}; the harness was last "
            f"verified against {expected_orca_version}",
            file=sys.stderr,
        )
    if (
        permission.status is not ValidationStatus.PASS
        or permission.strategy
        is not PermissionStrategy.READONLY_REPOSITORY
    ):
        raise PreflightError(
            "permission report is not a PASS result for strategy D"
        )
    environment_problems = permission_environment_problems(
        permission,
        arguments.harness_root,
        version,
    )
    if environment_problems:
        raise PreflightError(
            "permission report no longer matches this environment; "
            "re-run the permission spike: "
            + "; ".join(environment_problems)
        )
    for note in permission_report_notes(permission, version):
        print(f"[NOTE] {note}", file=sys.stderr)
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
    refresh_problem = permission_refresh_problem(
        arguments.harness_root,
        permission,
    )
    if refresh_problem:
        raise PreflightError(refresh_problem)
    if verify_coordinator:
        # Resume verifies (and rebinds) the coordinator terminal separately,
        # because a handle that died with the previous process must not make
        # the run unresumable.
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
