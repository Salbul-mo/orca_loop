from __future__ import annotations

from pathlib import Path

from .contracts import parse_permission_report
from .models import (
    LaunchProfile,
    PermissionFeasibilityReport,
    PermissionStrategy,
    Role,
    ValidationStatus,
)


class LaunchProfileError(ValueError):
    """Raised when a role launch profile is not permission-feasible."""


READ_ONLY_ROLES = {
    Role.PLANNER,
    Role.PLAN_REVIEWER,
    Role.CODE_REVIEWER,
    Role.CROSS_CONFIRMER,
}
CLAUDE_ROLES = {Role.PLANNER, Role.CODE_REVIEWER}
CODEX_ROLES = {
    Role.PLAN_REVIEWER,
    Role.IMPLEMENTER,
    Role.CROSS_CONFIRMER,
}


def _validate_path(path: Path, name: str) -> Path:
    if not path.is_absolute():
        raise LaunchProfileError(f"{name} must be absolute")
    resolved = path.resolve()
    if not resolved.exists():
        raise LaunchProfileError(f"{name} does not exist: {resolved}")
    return resolved


def _verify_permission_report(
    report: PermissionFeasibilityReport,
    expected_orca_version: str,
) -> None:
    if report.status is not ValidationStatus.PASS:
        raise LaunchProfileError("permission report is not PASS")
    if report.strategy is not PermissionStrategy.READONLY_REPOSITORY:
        raise LaunchProfileError(
            "only the live-verified strategy D may be used"
        )
    if report.orca_version != expected_orca_version:
        raise LaunchProfileError(
            "permission report Orca version does not match runtime"
        )
    canonical = Path(report.canonical_path)
    if not canonical.is_absolute() or not canonical.is_file():
        raise LaunchProfileError(
            "permission report canonical_path is invalid"
        )
    parsed = parse_permission_report(
        canonical.read_text(encoding="utf-8")
    )
    if parsed != report:
        raise LaunchProfileError(
            "permission report does not match canonical file"
        )


def build_launch_profile(
    role: Role,
    worktree: Path,
    step_input: Path,
    step_output: Path,
    permission_report: PermissionFeasibilityReport,
    *,
    expected_orca_version: str,
) -> LaunchProfile:
    root = _validate_path(worktree, "worktree")
    input_dir = _validate_path(step_input, "step_input")
    _validate_path(step_output, "step_output")
    _verify_permission_report(
        permission_report,
        expected_orca_version,
    )
    if role not in READ_ONLY_ROLES | {Role.IMPLEMENTER}:
        raise LaunchProfileError(f"unsupported role: {role.value}")

    if role in CLAUDE_ROLES:
        command = (
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            "Read,Write",
            "--add-dir",
            str(root),
            "--output-format",
            "json",
        )
    elif role in CODEX_ROLES:
        command = (
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(root),
            "--ephemeral",
            "--json",
            "-",
        )
    else:
        raise LaunchProfileError(f"unsupported role: {role.value}")

    if role in READ_ONLY_ROLES:
        writable_roots: tuple[Path, ...] = ()
    else:
        writable_roots = (root,)
    if any(path == input_dir.parent for path in writable_roots):
        raise LaunchProfileError(
            "launch profile cannot grant the entire step root"
        )
    return LaunchProfile(
        command=command,
        writable_roots=writable_roots,
        permission_report_digest=permission_report.report_digest,
    )
