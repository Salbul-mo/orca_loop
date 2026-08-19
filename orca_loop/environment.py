"""Capture and compare the environment a permission proof depends on.

The permission feasibility report used to be pinned to an exact Orca version,
which forced a full re-spike after every Orca update even though Orca does not
mediate file access at all: it creates terminals and routes orchestration
messages, while the read-only guarantee comes from the OS ACL that
``readonly.py`` applies and the launch flags that ``profiles.py`` builds.

This module blocks only platform and enforcement-code drift. Agent CLI
availability or version drift is reported as an informational note; a typed
permission failure observed during a real worker step separately creates the
refresh marker that blocks subsequent launches.
"""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import subprocess
from pathlib import Path

from .models import PermissionEnvironment


# Harness code whose content decides whether the proof still holds.
ENFORCEMENT_SOURCES = (
    "orca_loop/readonly.py",
    "orca_loop/profiles.py",
)
AGENT_CLIS = ("claude", "codex")
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)+")


def agent_cli_version(name: str) -> str | None:
    """Return the reported version of an agent CLI, or None when absent."""
    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            (executable, "--version"),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = VERSION_PATTERN.search(
        (completed.stdout or "") + " " + (completed.stderr or "")
    )
    return match.group(0) if match else None


def enforcement_digest(harness_root: Path) -> str:
    """Digest the harness code that implements the read-only strategy."""
    digest = hashlib.sha256()
    for relative in ENFORCEMENT_SOURCES:
        path = Path(harness_root) / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        # Normalize line endings so a checkout difference is not read as a
        # change in enforcement behaviour.
        digest.update(raw.replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def capture_environment(harness_root: Path) -> PermissionEnvironment:
    return PermissionEnvironment(
        platform=platform.system(),
        claude_cli=agent_cli_version("claude"),
        codex_cli=agent_cli_version("codex"),
        enforcement_digest=enforcement_digest(harness_root),
    )


def compare_environment(
    recorded: PermissionEnvironment,
    current: PermissionEnvironment,
    *,
    strict: bool = False,
) -> tuple[str, ...]:
    """Report platform or enforcement drift that invalidates the proof."""
    problems: list[str] = []
    if recorded.platform != current.platform:
        problems.append(
            f"platform changed: report {recorded.platform!r}, "
            f"current {current.platform!r}"
        )
    if recorded.enforcement_digest != current.enforcement_digest:
        problems.append(
            "read-only enforcement code changed since the report "
            f"({', '.join(ENFORCEMENT_SOURCES)})"
        )
    return tuple(problems)


def environment_notes(
    recorded: PermissionEnvironment,
    current: PermissionEnvironment,
) -> tuple[str, ...]:
    """Return informational agent CLI drift without invalidating proof."""
    notes: list[str] = []
    for name, recorded_value, current_value in (
        ("claude", recorded.claude_cli, current.claude_cli),
        ("codex", recorded.codex_cli, current.codex_cli),
    ):
        if recorded_value == current_value:
            continue
        if recorded_value is None or current_value is None:
            notes.append(
                f"{name} CLI availability changed: report "
                f"{recorded_value!r}, current {current_value!r}"
            )
            continue
        notes.append(
            f"{name} CLI version changed: report {recorded_value}, "
            f"current {current_value}"
        )
    return tuple(notes)


def describe_environment(value: PermissionEnvironment) -> dict[str, object]:
    return {
        "platform": value.platform,
        "claude_cli": value.claude_cli,
        "codex_cli": value.codex_cli,
        "enforcement_digest": value.enforcement_digest,
    }
