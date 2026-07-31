from __future__ import annotations

import hashlib
import os
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Mapping

from .models import (
    AffectedFile,
    AffectedFileOperation,
    DestructiveApproval,
    GuardReport,
    InputManifest,
    Role,
    SnapshotIdentity,
    StepWorkspace,
    TestExecutionPolicy,
    Violation,
)
from .transport import InputStagingError, verify_input_manifest


class GuardPathBoundaryError(ValueError):
    """Raised when a repository-relative path escapes its boundary."""


class GuardScopeViolationError(RuntimeError):
    """Raised when a delta cannot be safely classified."""


READ_ONLY_ROLES = {
    Role.PLANNER,
    Role.PLAN_REVIEWER,
    Role.CODE_REVIEWER,
    Role.CROSS_CONFIRMER,
}


def normalize_repo_path(value: str) -> str:
    if not value:
        raise GuardPathBoundaryError("repository path must be nonempty")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise GuardPathBoundaryError(
            f"invalid repository-relative path: {value!r}"
        )
    normalized = unicodedata.normalize("NFC", path.as_posix())
    if normalized in {"", "."}:
        raise GuardPathBoundaryError(
            f"invalid repository-relative path: {value!r}"
        )
    return normalized


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def capture_file_state(worktree: Path) -> dict[str, str]:
    root = worktree.resolve()
    completed = subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        shell=False,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GuardScopeViolationError(
            completed.stderr.decode("utf-8", "replace")
        )
    state: dict[str, str] = {}
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        path = normalize_repo_path(raw.decode("utf-8", "strict"))
        absolute = (root / Path(path)).resolve()
        try:
            absolute.relative_to(root)
        except ValueError as exc:
            raise GuardPathBoundaryError(
                f"Git path escaped worktree: {path}"
            ) from exc
        if absolute.is_symlink():
            raise GuardPathBoundaryError(
                f"symlink path is not allowed: {path}"
            )
        if absolute.is_file():
            state[path] = _sha256(absolute)
    return state


def _under(path: str, parent: str) -> bool:
    path_parts = PurePosixPath(path).parts
    parent_parts = PurePosixPath(parent).parts
    return (
        len(path_parts) >= len(parent_parts)
        and path_parts[: len(parent_parts)] == parent_parts
    )


def _approval_allows(
    path: str,
    operation: AffectedFileOperation,
    approval: DestructiveApproval | None,
) -> bool:
    if approval is None:
        return False
    return any(
        item.operation is operation
        and (
            normalize_repo_path(item.path) == path
            or (
                operation is AffectedFileOperation.DELETE
                and _under(path, normalize_repo_path(item.path))
            )
        )
        for item in approval.approved_operations
    )


def _planned_operation(
    path: str,
    operation: AffectedFileOperation,
    affected_files: tuple[AffectedFile, ...],
) -> bool:
    return any(
        item.operation is operation
        and (
            normalize_repo_path(item.path) == path
            or (
                operation is AffectedFileOperation.DELETE
                and _under(path, normalize_repo_path(item.path))
            )
        )
        for item in affected_files
    )


def guard_repository_delta(
    before_snapshot: SnapshotIdentity,
    after_snapshot: SnapshotIdentity,
    role: Role,
    affected_files: tuple[AffectedFile, ...],
    destructive_approval: DestructiveApproval | None,
    *,
    before_files: Mapping[str, str] | None,
    after_files: Mapping[str, str] | None,
) -> GuardReport:
    if before_snapshot == after_snapshot:
        return GuardReport(True, ())
    if before_files is None or after_files is None:
        return GuardReport(
            False,
            (
                Violation(
                    "unclassified_snapshot_delta",
                    None,
                    "file-state maps are required when snapshot changes",
                ),
            ),
        )
    before = {
        normalize_repo_path(path): digest
        for path, digest in before_files.items()
    }
    after = {
        normalize_repo_path(path): digest
        for path, digest in after_files.items()
    }
    added = set(after) - set(before)
    deleted = set(before) - set(after)
    modified = {
        path
        for path in set(before) & set(after)
        if before[path] != after[path]
    }
    if role in READ_ONLY_ROLES:
        paths = sorted(added | deleted | modified)
        return GuardReport(
            not paths,
            tuple(
                Violation(
                    "readonly_source_delta",
                    path,
                    f"{role.value} changed repository content",
                )
                for path in paths
            ),
        )
    if role is not Role.IMPLEMENTER:
        raise GuardScopeViolationError(f"unsupported role: {role.value}")

    violations: list[Violation] = []
    consumed_added: set[str] = set()
    consumed_deleted: set[str] = set()
    for planned in affected_files:
        if planned.operation is not AffectedFileOperation.RENAME:
            continue
        target = normalize_repo_path(planned.path)
        if planned.rename_from is None:
            raise GuardPathBoundaryError(
                f"rename missing source: {planned.path}"
            )
        source = normalize_repo_path(planned.rename_from)
        if source in deleted and target in added:
            if (
                destructive_approval is None
                or not any(
                    item.operation is AffectedFileOperation.RENAME
                    and normalize_repo_path(item.path) == target
                    and item.rename_from is not None
                    and normalize_repo_path(item.rename_from) == source
                    for item in destructive_approval.approved_operations
                )
            ):
                violations.append(
                    Violation(
                        "unapproved_rename",
                        target,
                        f"rename {source} -> {target} lacks exact approval",
                    )
                )
            consumed_added.add(target)
            consumed_deleted.add(source)

    for path in sorted(modified):
        if not _planned_operation(
            path,
            AffectedFileOperation.MODIFY,
            affected_files,
        ):
            violations.append(
                Violation(
                    "scope_violation",
                    path,
                    "modified file is not in approved plan scope",
                )
            )
    for path in sorted(added - consumed_added):
        if not _planned_operation(
            path,
            AffectedFileOperation.ADD,
            affected_files,
        ):
            violations.append(
                Violation(
                    "scope_violation",
                    path,
                    "added file is not in approved plan scope",
                )
            )
    for path in sorted(deleted - consumed_deleted):
        if not _planned_operation(
            path,
            AffectedFileOperation.DELETE,
            affected_files,
        ):
            violations.append(
                Violation(
                    "unplanned_deletion",
                    path,
                    "deleted file is not in approved plan scope",
                )
            )
        elif not _approval_allows(
            path,
            AffectedFileOperation.DELETE,
            destructive_approval,
        ):
            violations.append(
                Violation(
                    "unapproved_deletion",
                    path,
                    "planned deletion lacks destructive approval",
                )
            )
    return GuardReport(not violations, tuple(violations))


def guard_step_sandbox(
    step: StepWorkspace,
    manifest: InputManifest,
    artifact_paths: tuple[Path, ...],
    *,
    test_policy: TestExecutionPolicy | None,
    changed_test_paths: tuple[str, ...],
) -> GuardReport:
    violations: list[Violation] = []
    try:
        verify_input_manifest(step, manifest)
    except InputStagingError as exc:
        violations.append(
            Violation("input_tampered", None, str(exc))
        )
    output = step.output_dir.resolve()
    for artifact in artifact_paths:
        resolved = artifact.resolve()
        try:
            resolved.relative_to(output)
        except ValueError:
            violations.append(
                Violation(
                    "outbox_escape",
                    str(artifact),
                    "artifact is outside current step output directory",
                )
            )
            continue
        if artifact.is_symlink():
            violations.append(
                Violation(
                    "outbox_escape",
                    str(artifact),
                    "artifact path is a symlink",
                )
            )
    allowed = (
        ()
        if test_policy is None
        else tuple(
            normalize_repo_path(path)
            for path in test_policy.allowed_output_paths
        )
    )
    for raw_path in changed_test_paths:
        path = normalize_repo_path(raw_path)
        if not any(_under(path, allowed_path) for allowed_path in allowed):
            violations.append(
                Violation(
                    "test_output_scope",
                    path,
                    "test changed a path outside allowed output scope",
                )
            )
    return GuardReport(not violations, tuple(violations))
