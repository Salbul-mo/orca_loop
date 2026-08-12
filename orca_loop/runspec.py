"""Durable record of everything a run needs in order to be restarted.

Before this module existed, resuming a run required the operator to retype the
request path, permission report, test policy, timeouts and eight model/effort
values exactly as the original launch had them, with no file on disk recording
what those values were. ``run-manifest.json`` is that file: ``resume --run-id``
reads it and reconstructs the launch.

The request text is copied into the control directory as well, so a run stays
resumable even when the original request file is moved or deleted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .generation import AtomicWriteError, write_atomic_bytes
from .models import (
    AgentProvider,
    AgentRuntimeConfig,
    LoopConfig,
    WorkerHandle,
    WorkerKey,
    WorkerPool,
)


MANIFEST_NAME = "run-manifest.json"
REQUEST_COPY_NAME = "request.md"
MANIFEST_SCHEMA_VERSION = 1

LIMIT_KEYS = (
    "plan_consensus_round_limit",
    "code_consensus_round_limit",
    "test_fix_attempt_limit",
    "operational_retry_limit",
    "max_transition_count",
    "step_timeout_ms",
    "total_timeout_ms",
)


class ManifestError(RuntimeError):
    """Raised when a run manifest cannot be built, written, or trusted."""


@dataclass(frozen=True)
class FileRef:
    path: str
    digest: str | None


@dataclass(frozen=True)
class AgentRecord:
    worker_key: WorkerKey
    provider: AgentProvider
    requested_model: str | None
    model: str | None
    model_method: str
    requested_effort: str | None
    effort: str | None
    effort_method: str


@dataclass(frozen=True)
class RunManifest:
    schema_version: int
    run_id: str
    created_at: str
    harness_root: str
    worktree_path: str
    request: FileRef
    request_copy: str
    permission_report: FileRef
    test_policy: FileRef | None
    limits: Mapping[str, int]
    agents: tuple[AgentRecord, ...]
    orca_version: str
    coordinator_handle: str
    workers: tuple[tuple[WorkerKey, str], ...]

    def worker_handles(self) -> dict[WorkerKey, str]:
        return {key: handle for key, handle in self.workers}


def digest_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def digest_file(path: Path) -> str:
    try:
        return digest_bytes(path.read_bytes())
    except OSError as exc:
        raise ManifestError(f"cannot digest file: {path}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ordered_workers(
    pool: WorkerPool,
) -> tuple[tuple[WorkerKey, str], ...]:
    """Record worker handles in WorkerKey declaration order.

    Parsing rebuilds the tuple in that same order, so round-tripping a
    manifest compares equal.
    """
    handles = {item.worker_key: item.terminal_handle for item in pool.workers}
    return tuple(
        (worker, handles[worker])
        for worker in WorkerKey
        if worker in handles
    )


def copy_request(control_dir: Path, request_path: Path) -> tuple[Path, str]:
    """Store a byte-identical copy of the request inside the control dir."""
    try:
        raw = request_path.read_bytes()
    except OSError as exc:
        raise ManifestError(
            f"cannot read request file: {request_path}"
        ) from exc
    target = control_dir.resolve() / REQUEST_COPY_NAME
    if target.exists():
        existing = target.read_bytes()
        if existing != raw:
            raise ManifestError(
                "control request copy differs from the supplied request; "
                "start a new run instead of changing the request of an "
                "existing one"
            )
        return target, digest_bytes(raw)
    try:
        write_atomic_bytes(target, raw)
    except AtomicWriteError as exc:
        raise ManifestError(str(exc)) from exc
    return target, digest_bytes(raw)


def _agent_records(
    runtime: AgentRuntimeConfig | None,
    resolutions: tuple[object, ...],
) -> tuple[AgentRecord, ...]:
    by_worker = {
        item.worker_key: item
        for item in (() if runtime is None else runtime.agents)
    }
    resolution_by_worker = {
        getattr(item, "worker_key"): item for item in resolutions
    }
    records: list[AgentRecord] = []
    for worker in WorkerKey:
        options = by_worker.get(worker)
        if options is None:
            continue
        resolution = resolution_by_worker.get(worker)
        if resolution is None:
            records.append(
                AgentRecord(
                    worker_key=worker,
                    provider=options.provider,
                    requested_model=options.model,
                    model=options.model,
                    model_method="exact" if options.model else "inherit",
                    requested_effort=options.effort,
                    effort=options.effort,
                    effort_method="exact" if options.effort else "inherit",
                )
            )
            continue
        records.append(
            AgentRecord(
                worker_key=worker,
                provider=options.provider,
                requested_model=resolution.model.requested,
                model=options.model,
                model_method=resolution.model.method,
                requested_effort=resolution.effort.requested,
                effort=options.effort,
                effort_method=resolution.effort.method,
            )
        )
    return tuple(records)


def build_manifest(
    preflight,
    *,
    request_copy: Path,
    request_digest: str,
    coordinator_handle: str,
    pool: WorkerPool | None = None,
    created_at: str | None = None,
) -> RunManifest:
    arguments = preflight.arguments
    config: LoopConfig = arguments.config
    policy_path = config.test_policy_path
    test_policy = (
        None
        if policy_path is None
        else FileRef(str(policy_path), digest_file(policy_path))
    )
    workers: tuple[tuple[WorkerKey, str], ...] = ()
    if pool is not None:
        workers = _ordered_workers(pool)
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=arguments.run_id,
        created_at=_utc_now() if created_at is None else created_at,
        harness_root=str(arguments.harness_root),
        worktree_path=str(config.worktree_path),
        request=FileRef(str(config.request_path), request_digest),
        request_copy=str(request_copy),
        permission_report=FileRef(
            str(arguments.permission_report_path),
            digest_file(arguments.permission_report_path),
        ),
        test_policy=test_policy,
        limits={key: getattr(config, key) for key in LIMIT_KEYS},
        agents=_agent_records(
            preflight.agent_runtime,
            tuple(getattr(preflight, "agent_resolutions", ()) or ()),
        ),
        orca_version=preflight.orca_version,
        coordinator_handle=coordinator_handle,
        workers=workers,
    )


def _file_ref_value(value: FileRef | None) -> object:
    if value is None:
        return None
    return {"path": value.path, "digest": value.digest}


def manifest_value(manifest: RunManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "created_at": manifest.created_at,
        "harness_root": manifest.harness_root,
        "worktree_path": manifest.worktree_path,
        "request": _file_ref_value(manifest.request),
        "request_copy": manifest.request_copy,
        "permission_report": _file_ref_value(manifest.permission_report),
        "test_policy": _file_ref_value(manifest.test_policy),
        "limits": {key: manifest.limits[key] for key in LIMIT_KEYS},
        "agents": {
            record.worker_key.value: {
                "provider": record.provider.value,
                "requested_model": record.requested_model,
                "model": record.model,
                "model_method": record.model_method,
                "requested_effort": record.requested_effort,
                "effort": record.effort,
                "effort_method": record.effort_method,
            }
            for record in manifest.agents
        },
        "orca_version": manifest.orca_version,
        "coordinator_handle": manifest.coordinator_handle,
        "workers": {key.value: handle for key, handle in manifest.workers},
    }


def serialize_manifest(manifest: RunManifest) -> bytes:
    return (
        json.dumps(
            manifest_value(manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_manifest(control_dir: Path, manifest: RunManifest) -> Path:
    target = control_dir.resolve() / MANIFEST_NAME
    try:
        return write_atomic_bytes(target, serialize_manifest(manifest))
    except AtomicWriteError as exc:
        raise ManifestError(str(exc)) from exc


def _require(value: Mapping[str, object], key: str, kind: type):
    item = value.get(key)
    if not isinstance(item, kind) or (kind is str and not item):
        raise ManifestError(f"run manifest field {key!r} is invalid")
    return item


def _parse_file_ref(value: object, key: str) -> FileRef:
    if not isinstance(value, dict):
        raise ManifestError(f"run manifest field {key!r} must be an object")
    path = value.get("path")
    digest = value.get("digest")
    if not isinstance(path, str) or not path:
        raise ManifestError(f"run manifest field {key!r} has no path")
    if digest is not None and not isinstance(digest, str):
        raise ManifestError(f"run manifest field {key!r} has invalid digest")
    return FileRef(path, digest)


def parse_manifest(raw_text: str) -> RunManifest:
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"run manifest is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError("run manifest root must be an object")
    schema_version = value.get("schema_version")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"run manifest schema_version must be {MANIFEST_SCHEMA_VERSION}"
        )
    raw_limits = value.get("limits")
    if not isinstance(raw_limits, dict):
        raise ManifestError("run manifest limits must be an object")
    limits: dict[str, int] = {}
    for key in LIMIT_KEYS:
        item = raw_limits.get(key)
        if not isinstance(item, int) or isinstance(item, bool):
            raise ManifestError(f"run manifest limit {key!r} is invalid")
        limits[key] = item

    raw_agents = value.get("agents")
    if not isinstance(raw_agents, dict):
        raise ManifestError("run manifest agents must be an object")
    agents: list[AgentRecord] = []
    for worker in WorkerKey:
        item = raw_agents.get(worker.value)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ManifestError(
                f"run manifest agent {worker.value} must be an object"
            )
        try:
            provider = AgentProvider(item.get("provider"))
        except ValueError as exc:
            raise ManifestError(
                f"run manifest agent {worker.value} has invalid provider"
            ) from exc
        agents.append(
            AgentRecord(
                worker_key=worker,
                provider=provider,
                requested_model=item.get("requested_model"),
                model=item.get("model"),
                model_method=str(item.get("model_method", "exact")),
                requested_effort=item.get("requested_effort"),
                effort=item.get("effort"),
                effort_method=str(item.get("effort_method", "exact")),
            )
        )

    raw_workers = value.get("workers")
    if not isinstance(raw_workers, dict):
        raise ManifestError("run manifest workers must be an object")
    workers: list[tuple[WorkerKey, str]] = []
    for worker in WorkerKey:
        handle = raw_workers.get(worker.value)
        if handle is None:
            continue
        if not isinstance(handle, str) or not handle:
            raise ManifestError(
                f"run manifest worker {worker.value} handle is invalid"
            )
        workers.append((worker, handle))

    raw_policy = value.get("test_policy")
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=_require(value, "run_id", str),
        created_at=_require(value, "created_at", str),
        harness_root=_require(value, "harness_root", str),
        worktree_path=_require(value, "worktree_path", str),
        request=_parse_file_ref(value.get("request"), "request"),
        request_copy=_require(value, "request_copy", str),
        permission_report=_parse_file_ref(
            value.get("permission_report"),
            "permission_report",
        ),
        test_policy=(
            None
            if raw_policy is None
            else _parse_file_ref(raw_policy, "test_policy")
        ),
        limits=limits,
        agents=tuple(agents),
        orca_version=_require(value, "orca_version", str),
        coordinator_handle=_require(value, "coordinator_handle", str),
        workers=tuple(workers),
    )


def read_manifest(control_dir: Path) -> RunManifest | None:
    """Load the manifest, or return None for a run that predates it."""
    path = control_dir.resolve() / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read run manifest: {path}") from exc
    return parse_manifest(raw_text)


def update_terminals(
    control_dir: Path,
    manifest: RunManifest,
    *,
    coordinator_handle: str,
    pool: WorkerPool | None = None,
) -> RunManifest:
    workers = manifest.workers
    if pool is not None:
        workers = _ordered_workers(pool)
    updated = replace(
        manifest,
        coordinator_handle=coordinator_handle,
        workers=workers,
    )
    write_manifest(control_dir, updated)
    return updated


def verify_inputs(manifest: RunManifest) -> tuple[str, ...]:
    """Report every stored input that can no longer be trusted.

    A moved file whose digest still matches is fine; a file whose content
    changed is not, because the run's contracts are bound to those digests.
    """
    problems: list[str] = []

    copy_path = Path(manifest.request_copy)
    if not copy_path.is_file():
        problems.append(f"request copy is missing: {copy_path}")
    elif (
        manifest.request.digest is not None
        and digest_bytes(copy_path.read_bytes()) != manifest.request.digest
    ):
        problems.append(
            f"request copy digest changed: {copy_path}"
        )

    report_path = Path(manifest.permission_report.path)
    if not report_path.is_file():
        problems.append(
            f"permission report is missing: {report_path}"
        )
    elif (
        manifest.permission_report.digest is not None
        and digest_bytes(report_path.read_bytes())
        != manifest.permission_report.digest
    ):
        problems.append(
            f"permission report content changed: {report_path}"
        )

    if manifest.test_policy is not None:
        policy_path = Path(manifest.test_policy.path)
        if not policy_path.is_file():
            problems.append(f"test policy is missing: {policy_path}")
        elif (
            manifest.test_policy.digest is not None
            and digest_bytes(policy_path.read_bytes())
            != manifest.test_policy.digest
        ):
            problems.append(
                f"test policy content changed: {policy_path}"
            )
    return tuple(problems)


def manifest_to_arguments(
    manifest: RunManifest,
    *,
    harness_root: Path,
    accept_worktree_drift: bool = False,
    force_unlock: bool = False,
    strict_agent_runtime: bool = False,
):
    """Rebuild launch arguments from the manifest for ``resume``.

    The request copy inside the control directory is used rather than the
    original path: it is byte-identical to what the run started with, which
    keeps ``request_digest`` contracts valid even if the original moved.
    """
    from .config import RunArguments, validate_loop_config

    problems = verify_inputs(manifest)
    if problems:
        raise ManifestError("; ".join(problems))
    limits = manifest.limits
    config = validate_loop_config(
        LoopConfig(
            worktree_path=Path(manifest.worktree_path),
            request_path=Path(manifest.request_copy),
            coordinator_handle=manifest.coordinator_handle,
            test_policy_path=(
                None
                if manifest.test_policy is None
                else Path(manifest.test_policy.path)
            ),
            plan_consensus_round_limit=limits["plan_consensus_round_limit"],
            code_consensus_round_limit=limits["code_consensus_round_limit"],
            test_fix_attempt_limit=limits["test_fix_attempt_limit"],
            operational_retry_limit=limits["operational_retry_limit"],
            max_transition_count=limits["max_transition_count"],
            step_timeout_ms=limits["step_timeout_ms"],
            total_timeout_ms=limits["total_timeout_ms"],
        )
    )
    return RunArguments(
        run_id=manifest.run_id,
        harness_root=harness_root.resolve(),
        config=config,
        permission_report_path=Path(manifest.permission_report.path),
        resume=True,
        dry_run=False,
        agent_runtime_request=None,
        strict_agent_runtime=strict_agent_runtime,
        accept_worktree_drift=accept_worktree_drift,
        force_unlock=force_unlock,
    )


def worker_pool_from_manifest(manifest: RunManifest) -> WorkerPool | None:
    """Rebuild the recorded worker pool, or None when it is incomplete."""
    handles = manifest.worker_handles()
    if set(handles) != set(WorkerKey):
        return None
    selector = f"path:{manifest.worktree_path}"
    return WorkerPool(
        tuple(
            WorkerHandle(
                worker_key=worker,
                terminal_handle=handles[worker],
                worktree_id=selector,
                tab_id="",
                leaf_id="",
            )
            for worker in WorkerKey
        )
    )
