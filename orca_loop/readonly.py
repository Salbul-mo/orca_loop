from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class ReadOnlyMirrorError(RuntimeError):
    """Raised when an immutable reviewer repository cannot be prepared."""


EXCLUDED_TOP_LEVEL = {
    ".git",
    ".venv",
    "__pycache__",
    "runs",
}
EXCLUDED_NAMES = {
    "__pycache__",
    ".pytest_cache",
}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _copy_repository(source: Path, destination: Path) -> None:
    for current, directories, filenames in os.walk(source):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        if relative == Path("."):
            directories[:] = [
                item
                for item in directories
                if item not in EXCLUDED_TOP_LEVEL
            ]
        else:
            directories[:] = [
                item
                for item in directories
                if item not in EXCLUDED_NAMES
            ]
        destination_dir = destination / relative
        destination_dir.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            source_file = current_path / filename
            if source_file.is_symlink():
                raise ReadOnlyMirrorError(
                    f"repository mirror rejects symlink: {source_file}"
                )
            if not source_file.is_file():
                continue
            shutil.copy2(source_file, destination_dir / filename)


def _lock_windows(path: Path) -> None:
    identity_result = subprocess.run(
        ("whoami",),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    identity = identity_result.stdout.strip()
    if identity_result.returncode != 0 or not identity:
        raise ReadOnlyMirrorError(
            f"failed to resolve Windows identity: {identity_result.stderr}"
        )
    result = subprocess.run(
        (
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(RX)",
            "/T",
            "/C",
        ),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ReadOnlyMirrorError(
            f"failed to apply read-only ACL: {result.stderr[-4096:]}"
        )


def _lock_posix(path: Path) -> None:
    for current, directories, filenames in os.walk(path):
        current_path = Path(current)
        for filename in filenames:
            (current_path / filename).chmod(0o444)
        for directory in directories:
            (current_path / directory).chmod(0o555)
    path.chmod(0o555)


def prepare_readonly_mirror(
    worktree: Path,
    review_root: Path,
    generation: int,
    *,
    apply_permissions: bool = True,
) -> Path:
    source = worktree.resolve()
    destination_parent = review_root.resolve()
    if not source.is_dir() or not (source / ".git").exists():
        raise ReadOnlyMirrorError(
            f"worktree is not a Git repository: {source}"
        )
    if _inside(destination_parent, source):
        raise ReadOnlyMirrorError(
            "review_root must be outside the target worktree"
        )
    if generation < 0:
        raise ReadOnlyMirrorError("generation must be nonnegative")
    destination = destination_parent / f"repository-{generation}"
    if destination.exists():
        raise ReadOnlyMirrorError(
            f"read-only mirror already exists: {destination}"
        )
    destination.mkdir(parents=True)
    _copy_repository(source, destination)
    if apply_permissions:
        if os.name == "nt":
            _lock_windows(destination)
        else:
            _lock_posix(destination)
    return destination
