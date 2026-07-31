from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .models import BootstrapReport


DEFAULT_GITIGNORE = """\
__pycache__/
*.py[cod]
.coverage
.pytest_cache/
.venv/
dist/
build/
runs/
*.log
"""


class BootstrapError(RuntimeError):
    """Raised when local harness bootstrap fails."""


class OrcaRepositoryRegistrationError(BootstrapError):
    """Raised when Orca repository registration is ambiguous or invalid."""


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        tuple(argv),
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError(
            f"command failed ({completed.returncode}): {tuple(argv)!r}; "
            f"stderr={completed.stderr[-4096:]!r}"
        )
    return completed


def _validate_root(harness_root: Path) -> Path:
    if not harness_root.is_absolute():
        raise BootstrapError("harness_root must be absolute")
    resolved = harness_root.resolve()
    if not resolved.is_dir():
        raise BootstrapError(f"harness_root is not a directory: {resolved}")
    if resolved != harness_root:
        raise BootstrapError(
            "harness_root must already be the resolved workspace path"
        )
    return resolved


def bootstrap_repository(harness_root: Path) -> BootstrapReport:
    root = _validate_root(harness_root)
    if not (root / ".git").is_dir():
        _run(("git", "init"), cwd=root)

    for directory in ("orca_loop", "prompts", "tests", "docs", "runs"):
        (root / directory).mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            DEFAULT_GITIGNORE,
            encoding="utf-8",
            newline="\n",
        )
    package_init = root / "orca_loop" / "__init__.py"
    if not package_init.exists():
        package_init.write_text("", encoding="utf-8", newline="\n")

    _run(("git", "status", "--porcelain"), cwd=root)
    import_check = _run(
        (
            sys.executable,
            "-c",
            "import orca_loop; print(orca_loop.__name__)",
        ),
        cwd=root,
    )
    package_importable = import_check.stdout.strip() == "orca_loop"
    if not package_importable:
        raise BootstrapError("orca_loop import check returned unexpected output")
    return BootstrapReport(
        repo_initialized=True,
        package_importable=True,
        repo_id="",
        kind="",
    )


def _parse_cli_json(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OrcaRepositoryRegistrationError(
            "Orca returned malformed JSON"
        ) from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise OrcaRepositoryRegistrationError(
            f"Orca command did not return ok=true: {value!r}"
        )
    result = value.get("result")
    if not isinstance(result, dict):
        raise OrcaRepositoryRegistrationError(
            "Orca response is missing result object"
        )
    return result


def _same_resolved_path(candidate: object, expected: Path) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        return Path(candidate).resolve() == expected
    except OSError:
        return False


def register_orca_repository(
    harness_root: Path,
    orca_executable: str,
) -> BootstrapReport:
    root = _validate_root(harness_root)
    if not (root / ".git").is_dir():
        raise OrcaRepositoryRegistrationError(
            "harness_root must be a Git repository"
        )
    if not orca_executable:
        raise OrcaRepositoryRegistrationError(
            "orca_executable must be nonempty"
        )

    add_result = _run(
        (orca_executable, "repo", "add", "--path", str(root), "--json"),
        cwd=root,
    )
    _parse_cli_json(add_result)
    list_result = _run(
        (orca_executable, "repo", "list", "--json"),
        cwd=root,
    )
    result = _parse_cli_json(list_result)
    repos = result.get("repos")
    if not isinstance(repos, list):
        raise OrcaRepositoryRegistrationError(
            "Orca repo list response is missing repos"
        )
    matching = [
        item
        for item in repos
        if isinstance(item, dict)
        and _same_resolved_path(item.get("path"), root)
    ]
    if len(matching) != 1:
        record_ids = [
            str(item.get("id"))
            for item in matching
            if isinstance(item, dict)
        ]
        raise OrcaRepositoryRegistrationError(
            "BLOCKED: expected exactly one same-path Orca record; "
            f"found {len(matching)} ids={record_ids}"
        )
    record = matching[0]
    repo_id = record.get("id")
    kind = record.get("kind")
    if not isinstance(repo_id, str) or not repo_id:
        raise OrcaRepositoryRegistrationError(
            "Orca repository ID is missing"
        )
    if kind != "git":
        raise OrcaRepositoryRegistrationError(
            f"BLOCKED: Orca repository kind is {kind!r}, expected 'git'"
        )
    return BootstrapReport(
        repo_initialized=True,
        package_importable=True,
        repo_id=repo_id,
        kind=kind,
    )
