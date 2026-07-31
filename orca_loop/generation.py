from __future__ import annotations

import hashlib
import json
import os
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar, Union, get_args, get_origin, get_type_hints

from .models import (
    CommitManifest,
    ConsensusLedger,
    CoordinatorState,
)


class GenerationError(RuntimeError):
    """Base error for generation transaction failures."""


class GenerationMismatchError(GenerationError):
    """Raised when state and ledger generations are not monotonic."""


class AtomicWriteError(GenerationError):
    """Raised when an atomic state transaction cannot be verified."""


T = TypeVar("T")


def _internal_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {
            field.name: _internal_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_internal_value(item) for item in value]
    if isinstance(value, list):
        return [_internal_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _internal_value(item)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise AtomicWriteError(
        f"unsupported persistent type: {type(value).__name__}"
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _atomic_write_fsync(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise AtomicWriteError(f"failed atomic write: {path}") from exc


def write_atomic_bytes(path: Path, raw: bytes) -> Path:
    target = path.resolve()
    if path.is_symlink():
        raise AtomicWriteError(f"atomic write target must not be a symlink: {path}")
    if target.exists() and not target.is_file():
        raise AtomicWriteError(
            f"atomic write target must be a regular file: {target}"
        )
    _atomic_write_fsync(target, raw)
    try:
        persisted = target.read_bytes()
    except OSError as exc:
        raise AtomicWriteError(
            f"failed to verify atomic write: {target}"
        ) from exc
    if persisted != raw:
        raise AtomicWriteError(
            f"atomic write verification mismatch: {target}"
        )
    return target


def _decode(expected_type: Any, value: object, context: str) -> object:
    origin = get_origin(expected_type)
    args = get_args(expected_type)
    if origin in {Union, types.UnionType}:
        if value is None and type(None) in args:
            return None
        errors: list[Exception] = []
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _decode(candidate, value, context)
            except (AtomicWriteError, TypeError, ValueError) as exc:
                errors.append(exc)
        raise AtomicWriteError(
            f"{context} does not match any union member: {errors}"
        )
    if origin is tuple:
        if not isinstance(value, list):
            raise AtomicWriteError(f"{context} must be an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(
                _decode(args[0], item, f"{context}[{index}]")
                for index, item in enumerate(value)
            )
        if len(args) != len(value):
            raise AtomicWriteError(f"{context} tuple length mismatch")
        return tuple(
            _decode(item_type, item, f"{context}[{index}]")
            for index, (item_type, item) in enumerate(zip(args, value))
        )
    if expected_type is Path:
        if not isinstance(value, str):
            raise AtomicWriteError(f"{context} must be a path string")
        return Path(value)
    if isinstance(expected_type, type) and issubclass(expected_type, Enum):
        try:
            return expected_type(value)
        except (TypeError, ValueError) as exc:
            raise AtomicWriteError(
                f"{context} has invalid enum value"
            ) from exc
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        if not isinstance(value, dict):
            raise AtomicWriteError(f"{context} must be an object")
        hints = get_type_hints(expected_type)
        expected_fields = {field.name for field in fields(expected_type)}
        if set(value) != expected_fields:
            raise AtomicWriteError(
                f"{context} fields mismatch: expected "
                f"{sorted(expected_fields)}, got {sorted(value)}"
            )
        kwargs = {
            field.name: _decode(
                hints[field.name],
                value[field.name],
                f"{context}.{field.name}",
            )
            for field in fields(expected_type)
        }
        return expected_type(**kwargs)
    if expected_type is bool:
        if not isinstance(value, bool):
            raise AtomicWriteError(f"{context} must be boolean")
        return value
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise AtomicWriteError(f"{context} must be integer")
        return value
    if expected_type is str:
        if not isinstance(value, str):
            raise AtomicWriteError(f"{context} must be string")
        return value
    if expected_type is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AtomicWriteError(f"{context} must be numeric")
        return float(value)
    if expected_type is Any:
        return value
    raise AtomicWriteError(
        f"{context} has unsupported expected type {expected_type!r}"
    )


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicWriteError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise AtomicWriteError(f"JSON root must be object: {path}")
    return value


def _load_manifest(control_dir: Path) -> CommitManifest | None:
    path = control_dir / "commit.json"
    if not path.exists():
        return None
    return _decode(
        CommitManifest,
        _load_json(path),
        "commit",
    )  # type: ignore[return-value]


def commit_generation(
    control_dir: Path,
    state: CoordinatorState,
    ledger: ConsensusLedger,
    *,
    before_manifest: Callable[[], None] | None = None,
) -> CommitManifest:
    control = control_dir.resolve()
    manifest = _load_manifest(control)
    current_generation = (
        -1 if manifest is None else manifest.committed_generation
    )
    if state.generation != current_generation + 1:
        raise GenerationMismatchError(
            f"state generation {state.generation} is not "
            f"current+1 ({current_generation + 1})"
        )
    if ledger.generation != state.generation:
        raise GenerationMismatchError(
            "state and ledger generation must match"
        )
    if ledger.run_id != state.run_id:
        raise GenerationMismatchError(
            "state and ledger run_id must match"
        )

    state_raw = _canonical_bytes(_internal_value(state)) + b"\n"
    ledger_raw = _canonical_bytes(_internal_value(ledger)) + b"\n"
    state_path = control / f"state.{state.generation}.json"
    ledger_path = control / f"ledger.{state.generation}.json"
    _atomic_write_fsync(state_path, state_raw)
    _atomic_write_fsync(ledger_path, ledger_raw)
    state_digest = _digest(state_raw)
    ledger_digest = _digest(ledger_raw)
    committed = CommitManifest(
        committed_generation=state.generation,
        state_digest=state_digest,
        ledger_digest=ledger_digest,
    )
    if before_manifest is not None:
        before_manifest()
    _atomic_write_fsync(
        control / "commit.json",
        _canonical_bytes(_internal_value(committed)) + b"\n",
    )
    loaded_state, loaded_ledger, loaded_manifest = load_committed(control)
    if (
        loaded_state != state
        or loaded_ledger != ledger
        or loaded_manifest != committed
    ):
        raise AtomicWriteError(
            "generation reread does not match committed values"
        )
    return committed


def load_committed(
    control_dir: Path,
) -> tuple[CoordinatorState, ConsensusLedger, CommitManifest]:
    control = control_dir.resolve()
    manifest = _load_manifest(control)
    if manifest is None:
        raise AtomicWriteError("commit.json does not exist")
    state_path = control / f"state.{manifest.committed_generation}.json"
    ledger_path = control / f"ledger.{manifest.committed_generation}.json"
    try:
        state_raw = state_path.read_bytes()
        ledger_raw = ledger_path.read_bytes()
    except OSError as exc:
        raise AtomicWriteError(
            "committed generation file is missing"
        ) from exc
    if _digest(state_raw) != manifest.state_digest:
        raise AtomicWriteError("committed state digest mismatch")
    if _digest(ledger_raw) != manifest.ledger_digest:
        raise AtomicWriteError("committed ledger digest mismatch")
    state = _decode(
        CoordinatorState,
        _load_json(state_path),
        "state",
    )
    ledger = _decode(
        ConsensusLedger,
        _load_json(ledger_path),
        "ledger",
    )
    assert isinstance(state, CoordinatorState)
    assert isinstance(ledger, ConsensusLedger)
    if (
        state.generation != manifest.committed_generation
        or ledger.generation != manifest.committed_generation
        or state.run_id != ledger.run_id
    ):
        raise GenerationMismatchError(
            "committed generation provenance mismatch"
        )
    return state, ledger, manifest
