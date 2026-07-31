from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .contracts import ContractViolationError, canonical_json_bytes
from .models import (
    ArtifactKind,
    DigestEntry,
    DispatchHandle,
    InputManifest,
    PromotedArtifact,
    StagedInput,
    StepWorkspace,
    WorkerDonePayload,
)


MAX_INPUT_BYTES = 10 * 1024 * 1024
MANIFEST_NAME = "inputs.sha256"


class InputStagingError(RuntimeError):
    """Base error for staged input and artifact transport."""


class TransportPathBoundaryError(InputStagingError):
    """Raised when a staged path escapes its step boundary."""


class TransportProvenanceError(InputStagingError):
    """Raised when promoted output differs from dispatch provenance."""


class ScopeViolationError(InputStagingError):
    """Raised when an artifact path is outside the current outbox."""


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _valid_basename(name: str) -> bool:
    candidate = Path(name)
    return (
        bool(name)
        and candidate.name == name
        and not candidate.is_absolute()
        and name not in {".", "..", MANIFEST_NAME}
        and "/" not in name
        and "\\" not in name
    )


def _input_bytes(staged: StagedInput) -> bytes:
    if (staged.source_path is None) == (staged.inline_bytes is None):
        raise InputStagingError(
            f"{staged.name} must provide exactly one input source"
        )
    if staged.source_path is not None:
        source = staged.source_path.resolve()
        if not source.is_file() or source.is_symlink():
            raise InputStagingError(
                f"input source must be a regular file: {source}"
            )
        return source.read_bytes()
    assert staged.inline_bytes is not None
    return bytes(staged.inline_bytes)


def _manifest_value(entries: tuple[DigestEntry, ...]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "entries": [
            {"path": entry.path, "digest": entry.digest}
            for entry in entries
        ],
    }


def stage_inputs(
    step: StepWorkspace,
    staged_inputs: tuple[StagedInput, ...],
) -> InputManifest:
    if (step.root / "binding.json").exists():
        raise InputStagingError(
            "step already has task or dispatch binding"
        )
    names = tuple(item.name for item in staged_inputs)
    if len(set(names)) != len(names):
        raise InputStagingError("duplicate staged input name")
    if not all(_valid_basename(name) for name in names):
        raise TransportPathBoundaryError(
            "staged input name must be a safe basename"
        )

    prepared: list[tuple[str, bytes]] = []
    total = 0
    for item in staged_inputs:
        raw = _input_bytes(item)
        total += len(raw)
        if total > MAX_INPUT_BYTES:
            raise InputStagingError(
                f"staged inputs exceed {MAX_INPUT_BYTES} bytes"
            )
        prepared.append((item.name, raw))

    for name, raw in prepared:
        _write_atomic(step.input_dir / name, raw)
    entries = tuple(
        DigestEntry(path=name, digest=_digest(raw))
        for name, raw in sorted(
            prepared,
            key=lambda item: item[0].encode("utf-8"),
        )
    )
    value = _manifest_value(entries)
    manifest_digest = _digest(canonical_json_bytes(value))
    persisted = {
        **value,
        "manifest_digest": manifest_digest,
    }
    _write_atomic(
        step.input_dir / MANIFEST_NAME,
        canonical_json_bytes(persisted) + b"\n",
    )
    manifest = InputManifest(entries, manifest_digest)
    verify_input_manifest(step, manifest)
    return manifest


def load_input_manifest(step: StepWorkspace) -> InputManifest:
    path = step.input_dir / MANIFEST_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputStagingError(f"invalid input manifest: {path}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "entries", "manifest_digest"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("entries"), list)
        or not isinstance(value.get("manifest_digest"), str)
    ):
        raise InputStagingError("input manifest schema mismatch")
    entries: list[DigestEntry] = []
    for index, entry in enumerate(value["entries"]):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "digest"}
            or not isinstance(entry["path"], str)
            or not isinstance(entry["digest"], str)
        ):
            raise InputStagingError(
                f"input manifest entry {index} is invalid"
            )
        entries.append(DigestEntry(entry["path"], entry["digest"]))
    manifest = InputManifest(
        entries=tuple(entries),
        manifest_digest=value["manifest_digest"],
    )
    verify_input_manifest(step, manifest)
    return manifest


def verify_input_manifest(
    step: StepWorkspace,
    manifest: InputManifest,
) -> None:
    ordered = tuple(
        sorted(manifest.entries, key=lambda item: item.path.encode("utf-8"))
    )
    if ordered != manifest.entries:
        raise InputStagingError("input manifest entries are not sorted")
    value = _manifest_value(manifest.entries)
    if _digest(canonical_json_bytes(value)) != manifest.manifest_digest:
        raise InputStagingError("input manifest digest mismatch")
    expected_names = {entry.path for entry in manifest.entries}
    actual_names = {
        path.name
        for path in step.input_dir.iterdir()
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual_names != expected_names:
        raise InputStagingError(
            "input directory contents differ from manifest"
        )
    for entry in manifest.entries:
        path = step.input_dir / entry.path
        if path.is_symlink() or not path.is_file():
            raise InputStagingError(
                f"manifest input is not a regular file: {entry.path}"
            )
        if _digest(path.read_bytes()) != entry.digest:
            raise InputStagingError(
                f"staged input digest mismatch: {entry.path}"
            )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path, stop: Path) -> bool:
    current = path
    stop_resolved = stop.resolve()
    while True:
        if current.is_symlink():
            return True
        if current.resolve() == stop_resolved:
            return False
        if current.parent == current:
            return True
        current = current.parent


def promote_artifact(
    payload: WorkerDonePayload,
    active: DispatchHandle,
    step: StepWorkspace,
    manifest: InputManifest,
    artifact_dir: Path,
    artifact_kind: ArtifactKind,
    parser: Callable[[str], object],
) -> PromotedArtifact:
    if payload.task_id != active.task_id:
        raise TransportProvenanceError("worker_done task_id mismatch")
    if payload.dispatch_id != active.dispatch_id:
        raise TransportProvenanceError("worker_done dispatch_id mismatch")
    report_path = Path(payload.report_path)
    if not report_path.is_absolute():
        raise ScopeViolationError("report_path must be absolute")
    if (
        not _inside(report_path, step.output_dir)
        or _has_symlink_component(report_path, step.output_dir)
        or not report_path.is_file()
    ):
        raise ScopeViolationError(
            "report_path must be a regular non-symlink file in step outbox"
        )
    raw = report_path.read_bytes()
    actual_digest = _digest(raw)
    if actual_digest != payload.artifact_digest:
        raise ContractViolationError("artifact digest mismatch")
    try:
        raw_text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ContractViolationError(
            "artifact must be strict UTF-8"
        ) from exc
    parser(raw_text)
    verify_input_manifest(step, manifest)

    destination = artifact_dir.resolve() / f"{artifact_kind.value}.json"
    if not _inside(destination, artifact_dir.resolve()):
        raise ScopeViolationError("artifact destination escaped artifact_dir")
    _write_atomic(destination, raw)
    if _digest(destination.read_bytes()) != actual_digest:
        raise InputStagingError(
            "promoted artifact digest verification failed"
        )
    return PromotedArtifact(
        canonical_path=destination,
        raw_text=raw_text,
        artifact_digest=actual_digest,
        artifact_kind=artifact_kind,
    )
