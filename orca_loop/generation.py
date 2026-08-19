from __future__ import annotations

import hashlib
import json
import os
from dataclasses import MISSING, replace
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar, Union, get_args, get_origin, get_type_hints

from .models import (
    CommitManifest,
    ConsensusLedger,
    CoordinatorState,
    MutationKind,
    MutationPhase,
    MutationRecord,
    MutationStore,
    DeliveryReceipt,
    InboxState,
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
        dataclass_fields = tuple(fields(expected_type))
        expected_fields = {field.name for field in dataclass_fields}
        missing = expected_fields - set(value)
        unexpected = set(value) - expected_fields
        required_missing = {
            field.name
            for field in dataclass_fields
            if field.name in missing
            and field.default is MISSING
            and field.default_factory is MISSING
        }
        if unexpected or required_missing:
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
            for field in dataclass_fields
            if field.name in value
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
    state_value = _load_json(state_path)
    state_schema = state_value.get("schema_version")
    if state_schema not in {1, 2}:
        raise AtomicWriteError(
            f"unsupported coordinator state schema_version: {state_schema!r}"
        )
    legacy_state = (
        state_schema == 1
        and "validation_lineage" not in state_value
        and "pending_review" not in state_value
    )
    state = _decode(CoordinatorState, state_value, "state")
    if legacy_state:
        state = replace(state, schema_version=2)
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


MUTATION_STORE_NAME = "orchestration-operations.json"
_PHASE_ORDER = {
    MutationPhase.INTENT: 0,
    MutationPhase.APPLIED: 1,
    MutationPhase.COMMITTED: 2,
}


def _mutation_path(control_dir: Path) -> Path:
    control = control_dir.resolve()
    path = (control / MUTATION_STORE_NAME).resolve()
    if path.parent != control:
        raise AtomicWriteError("mutation store escaped the control directory")
    return path


def read_mutations(control_dir: Path) -> tuple[MutationRecord, ...]:
    """Return every tracked mutation, oldest first."""
    path = _mutation_path(control_dir)
    if not path.exists():
        return ()
    store = _decode(MutationStore, _load_json(path), "mutation_store")
    return store.records  # type: ignore[union-attr]


def find_mutation(
    control_dir: Path,
    request_id: str,
) -> MutationRecord | None:
    for record in read_mutations(control_dir):
        if record.request_id == request_id:
            return record
    return None


def unresolved_mutation(
    control_dir: Path,
    kind: MutationKind,
    *,
    step_id: str | None,
) -> MutationRecord | None:
    """Return the mutation of this kind that never reached COMMITTED.

    A restart replays exactly this record instead of issuing a fresh mutation,
    which is what keeps a lost response from creating a duplicate.
    """
    for record in read_mutations(control_dir):
        if (
            record.kind is kind
            and record.step_id == step_id
            and record.phase is not MutationPhase.COMMITTED
        ):
            return record
    return None


def write_mutation(
    control_dir: Path,
    record: MutationRecord,
) -> MutationRecord:
    """Persist a new mutation or a legal forward transition of an existing one."""
    if not record.request_id:
        raise GenerationMismatchError("mutation request ID must be nonempty")
    if record.generation < 0:
        raise GenerationMismatchError("mutation generation must be >= 0")
    if not record.canonical_argv:
        raise GenerationMismatchError("mutation argv must be nonempty")
    path = _mutation_path(control_dir)
    existing = read_mutations(control_dir)
    updated: list[MutationRecord] = []
    replaced = False
    for item in existing:
        if item.request_id != record.request_id:
            updated.append(item)
            continue
        # Orca binds the request ID to the first attempt's argv, so a local
        # record that drifts from it would replay into a rejection.
        if (
            item.kind is not record.kind
            or item.run_id != record.run_id
            or item.step_id != record.step_id
            or item.canonical_argv != record.canonical_argv
        ):
            raise GenerationMismatchError(
                "mutation record fields are immutable once written"
            )
        if _PHASE_ORDER[record.phase] < _PHASE_ORDER[item.phase]:
            raise GenerationMismatchError(
                f"mutation phase cannot move {item.phase} -> {record.phase}"
            )
        updated.append(record)
        replaced = True
    if not replaced:
        updated.append(record)
    store = MutationStore(schema_version=1, records=tuple(updated))
    write_atomic_bytes(
        path,
        _canonical_bytes(_internal_value(store)) + b"\n",
    )
    reread = find_mutation(control_dir, record.request_id)
    if reread != record:
        raise AtomicWriteError("mutation record reread does not match")
    return record


INBOX_STORE_NAME = "inbox.json"
# Bounded so a long run cannot grow the control file without limit while still
# keeping enough history to recognise a replayed delivery after a restart.
MAX_INBOX_RECEIPTS = 64
MAX_PROMOTED_IDS = 512


def _inbox_path(control_dir: Path) -> Path:
    control = control_dir.resolve()
    path = (control / INBOX_STORE_NAME).resolve()
    if path.parent != control:
        raise AtomicWriteError("inbox store escaped the control directory")
    return path


def read_inbox(control_dir: Path) -> InboxState:
    path = _inbox_path(control_dir)
    if not path.exists():
        return InboxState(
            schema_version=1,
            receipts=(),
            promoted_message_ids=(),
        )
    return _decode(  # type: ignore[return-value]
        InboxState,
        _load_json(path),
        "inbox",
    )


def write_inbox(control_dir: Path, state: InboxState) -> InboxState:
    bounded = InboxState(
        schema_version=1,
        receipts=state.receipts[-MAX_INBOX_RECEIPTS:],
        promoted_message_ids=(
            state.promoted_message_ids[-MAX_PROMOTED_IDS:]
        ),
    )
    write_atomic_bytes(
        _inbox_path(control_dir),
        _canonical_bytes(_internal_value(bounded)) + b"\n",
    )
    if read_inbox(control_dir) != bounded:
        raise AtomicWriteError("inbox reread does not match")
    return bounded


def find_receipt(
    control_dir: Path,
    delivery_id: str,
) -> DeliveryReceipt | None:
    for receipt in read_inbox(control_dir).receipts:
        if receipt.delivery_id == delivery_id:
            return receipt
    return None


def record_receipt(
    control_dir: Path,
    receipt: DeliveryReceipt,
) -> DeliveryReceipt:
    """Store a delivery and its classifications, replacing any earlier copy."""
    if len(receipt.messages) != len(receipt.classifications):
        raise GenerationMismatchError(
            "every delivered message needs exactly one classification"
        )
    state = read_inbox(control_dir)
    kept = tuple(
        item
        for item in state.receipts
        if item.delivery_id != receipt.delivery_id
    )
    write_inbox(
        control_dir,
        replace(state, receipts=(*kept, receipt)),
    )
    return receipt


def mark_promoted(
    control_dir: Path,
    message_ids: tuple[str, ...],
) -> None:
    """Record which messages already became domain events.

    A crash between promoting a worker_done and acknowledging its delivery
    replays that message; this index is what lets the replay be recognised as
    a duplicate instead of being promoted twice or dropped silently.
    """
    if not message_ids:
        return
    state = read_inbox(control_dir)
    known = set(state.promoted_message_ids)
    added = tuple(item for item in message_ids if item not in known)
    if not added:
        return
    write_inbox(
        control_dir,
        replace(
            state,
            promoted_message_ids=(*state.promoted_message_ids, *added),
        ),
    )


def is_promoted(control_dir: Path, message_id: str) -> bool:
    return message_id in set(read_inbox(control_dir).promoted_message_ids)
