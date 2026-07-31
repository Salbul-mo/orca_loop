from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import unicodedata
from pathlib import Path
from typing import Sequence

from .contracts import canonical_json_bytes
from .models import (
    AffectedFile,
    AffectedFileOperation,
    FrozenReview,
    ScopeManifest,
    SnapshotIdentity,
)


class SnapshotError(RuntimeError):
    """Base error for Git snapshot capture and materialization."""


class GitCommandError(SnapshotError):
    """Raised when a read-only Git snapshot command fails."""


class SnapshotChangedError(SnapshotError):
    """Raised when source changes during frozen review creation."""


class SnapshotPathBoundaryError(SnapshotError):
    """Raised when a snapshot path escapes the target worktree."""


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_component(tag: str, data: bytes) -> bytes:
    tag_bytes = tag.encode("utf-8")
    return (
        struct.pack(">I", len(tag_bytes))
        + tag_bytes
        + struct.pack(">Q", len(data))
        + data
    )


def canonical_content(raw: bytes) -> bytes:
    if b"\0" in raw:
        return b"B" + raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return b"B" + raw
    return b"T" + text.replace("\r\n", "\n").encode("utf-8")


def _git(worktree: Path, argv: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(worktree), *argv),
        shell=False,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GitCommandError(
            f"git command failed ({completed.returncode}): {tuple(argv)!r}; "
            f"stderr={completed.stderr[-4096:].decode('utf-8', 'replace')!r}"
        )
    return completed.stdout


def _normalize_relative_path(raw_path: str, worktree: Path) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SnapshotPathBoundaryError(
            f"path must be repository-relative: {raw_path!r}"
        )
    normalized = unicodedata.normalize(
        "NFC",
        candidate.as_posix(),
    )
    resolved = (worktree / Path(normalized)).resolve()
    try:
        resolved.relative_to(worktree)
    except ValueError as exc:
        raise SnapshotPathBoundaryError(
            f"path escaped worktree: {raw_path!r}"
        ) from exc
    return normalized


def capture_snapshot(worktree: Path) -> SnapshotIdentity:
    root = worktree.resolve()
    if not root.is_dir():
        raise SnapshotError(f"worktree is not a directory: {root}")
    top_level = _git(root, ("rev-parse", "--show-toplevel")).decode(
        "utf-8",
        "strict",
    ).strip()
    if Path(top_level).resolve() != root:
        raise SnapshotError(
            f"worktree must be the Git top-level directory: {top_level}"
        )
    base_head = _git(root, ("rev-parse", "HEAD")).decode(
        "ascii",
        "strict",
    ).strip().lower()
    if not re_full_hex(base_head, 40, 64):
        raise SnapshotError(f"invalid Git HEAD: {base_head!r}")

    tracked = canonical_content(_git(root, ("diff", "--binary")))
    staged = canonical_content(
        _git(root, ("diff", "--cached", "--binary"))
    )
    raw_paths = _git(
        root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
    )
    decoded_paths = [
        item.decode("utf-8", "strict")
        for item in raw_paths.split(b"\0")
        if item
    ]
    normalized_paths = [
        _normalize_relative_path(item, root)
        for item in decoded_paths
    ]
    normalized_paths.sort(key=lambda item: item.encode("utf-8"))

    untracked_entries: list[tuple[str, str]] = []
    encoded_entries: list[bytes] = []
    for path in normalized_paths:
        raw = (root / Path(path)).read_bytes()
        content = canonical_content(raw)
        entry = canonical_component("path", path.encode("utf-8"))
        entry += canonical_component("content", content)
        encoded_entries.append(entry)
        untracked_entries.append((path, _sha256(content)))

    snapshot_bytes = b"orca-snapshot-v1\0"
    snapshot_bytes += canonical_component(
        "base_head",
        base_head.encode("ascii"),
    )
    snapshot_bytes += canonical_component("tracked_diff", tracked)
    snapshot_bytes += canonical_component("staged_diff", staged)
    for entry in encoded_entries:
        snapshot_bytes += canonical_component("untracked", entry)
    return SnapshotIdentity(
        base_head=base_head,
        tracked_diff_digest=_sha256(tracked),
        staged_diff_digest=_sha256(staged),
        untracked=tuple(untracked_entries),
        snapshot_digest=_sha256(snapshot_bytes),
    )


def re_full_hex(value: str, minimum: int, maximum: int) -> bool:
    return (
        minimum <= len(value) <= maximum
        and all(character in "0123456789abcdef" for character in value)
    )


def _normalize_affected(
    affected_files: tuple[AffectedFile, ...],
    worktree: Path,
    destructive_approval_digest: str | None,
) -> tuple[AffectedFile, ...]:
    normalized: list[AffectedFile] = []
    for affected in affected_files:
        path = _normalize_relative_path(affected.path, worktree)
        rename_from = (
            None
            if affected.rename_from is None
            else _normalize_relative_path(affected.rename_from, worktree)
        )
        if affected.operation in {
            AffectedFileOperation.DELETE,
            AffectedFileOperation.RENAME,
        } and not destructive_approval_digest:
            raise SnapshotError(
                "delete or rename requires destructive approval digest"
            )
        normalized.append(
            AffectedFile(
                path=path,
                operation=affected.operation,
                rename_from=rename_from,
            )
        )
    return tuple(sorted(normalized, key=lambda item: item.path.encode("utf-8")))


def _complete_diff(worktree: Path) -> bytes:
    tracked = _git(worktree, ("diff", "--binary"))
    staged = _git(worktree, ("diff", "--cached", "--binary"))
    raw_paths = _git(
        worktree,
        ("ls-files", "--others", "--exclude-standard", "-z"),
    )
    parts = [
        b"ORCA FROZEN REVIEW DIFF v1\n",
        b"\n--- TRACKED ---\n",
        tracked,
        b"\n--- STAGED ---\n",
        staged,
        b"\n--- UNTRACKED (base64) ---\n",
    ]
    paths = sorted(
        (
            _normalize_relative_path(
                item.decode("utf-8", "strict"),
                worktree,
            )
            for item in raw_paths.split(b"\0")
            if item
        ),
        key=lambda item: item.encode("utf-8"),
    )
    for path in paths:
        content = (worktree / Path(path)).read_bytes()
        parts.extend(
            (
                b"path:",
                path.encode("utf-8"),
                b"\nbytes-base64:",
                base64.b64encode(content),
                b"\n",
            )
        )
    return b"".join(parts)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def materialize_frozen_review(
    worktree: Path,
    expected_snapshot: SnapshotIdentity,
    affected_files: tuple[AffectedFile, ...],
    review_dir: Path,
    *,
    destructive_approval_digest: str | None,
) -> FrozenReview:
    root = worktree.resolve()
    before = capture_snapshot(root)
    if before != expected_snapshot:
        raise SnapshotChangedError(
            "worktree snapshot changed before materialization"
        )
    normalized = _normalize_affected(
        affected_files,
        root,
        destructive_approval_digest,
    )
    scope = ScopeManifest(
        snapshot_digest=expected_snapshot.snapshot_digest,
        affected_files=normalized,
        destructive_approval_digest=destructive_approval_digest,
    )
    scope_value = {
        "snapshot_digest": scope.snapshot_digest,
        "affected_files": [
            {
                "path": item.path,
                "operation": item.operation.value,
                "rename_from": item.rename_from,
            }
            for item in scope.affected_files
        ],
        "destructive_approval_digest": scope.destructive_approval_digest,
    }
    diff_bytes = _complete_diff(root)
    manifest_bytes = canonical_json_bytes(scope_value) + b"\n"
    after = capture_snapshot(root)
    if after != before:
        raise SnapshotChangedError(
            "worktree snapshot changed during materialization"
        )

    output = review_dir.resolve()
    diff_path = output / "frozen.diff"
    manifest_path = output / "scope-manifest.json"
    _write_atomic(diff_path, diff_bytes)
    _write_atomic(manifest_path, manifest_bytes)
    final = capture_snapshot(root)
    if final != before:
        raise SnapshotChangedError(
            "review output changed the target worktree snapshot"
        )
    return FrozenReview(
        diff_path=diff_path,
        manifest_path=manifest_path,
        snapshot_digest=expected_snapshot.snapshot_digest,
    )
