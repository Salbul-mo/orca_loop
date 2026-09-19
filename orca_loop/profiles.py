from __future__ import annotations

import json
from pathlib import Path

from .contracts import default_agent_provider
from .models import (
    AgentProvider,
    AgentRuntimeOptions,
    LaunchProfile,
    PermissionProfile,
    Role,
    WorkerKey,
)


class LaunchProfileError(ValueError):
    """Raised when a role launch profile is not permission-feasible."""


ROLE_RUNTIME_WORKERS = {
    Role.PLANNER: WorkerKey.CLAUDE_PLANNER,
    Role.PLAN_REVIEWER: WorkerKey.CODEX_REVIEW,
    Role.IMPLEMENTER: WorkerKey.CODEX_IMPLEMENTER,
    Role.CODE_REVIEWER: WorkerKey.CLAUDE_CODE_REVIEW,
    Role.CROSS_CONFIRMER: WorkerKey.CODEX_REVIEW,
}


def _validate_path(path: Path, name: str) -> Path:
    if not path.is_absolute():
        raise LaunchProfileError(f"{name} must be absolute")
    resolved = path.resolve()
    if not resolved.exists():
        raise LaunchProfileError(f"{name} does not exist: {resolved}")
    return resolved


def build_launch_profile(
    role: Role,
    permission_profile: PermissionProfile,
    worktree: Path,
    step_input: Path,
    step_output: Path,
    *,
    runtime_options: AgentRuntimeOptions | None = None,
) -> LaunchProfile:
    root = _validate_path(worktree, "worktree")
    input_dir = _validate_path(step_input, "step_input")
    _validate_path(step_output, "step_output")
    if role not in ROLE_RUNTIME_WORKERS:
        raise LaunchProfileError(f"unsupported role: {role.value}")
    if (
        runtime_options is not None
        and runtime_options.worker_key is not ROLE_RUNTIME_WORKERS[role]
    ):
        raise LaunchProfileError(
            "runtime options worker does not match role"
        )
    provider = (
        default_agent_provider(ROLE_RUNTIME_WORKERS[role])
        if runtime_options is None
        else runtime_options.provider
    )

    if provider is AgentProvider.CLAUDE:
        runtime_command: tuple[str, ...] = ()
        if runtime_options is not None:
            if runtime_options.model is not None:
                runtime_command += ("--model", runtime_options.model)
            if runtime_options.effort is not None:
                runtime_command += ("--effort", runtime_options.effort)
        command = (
            "claude",
            "-p",
            *runtime_command,
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            "Read,Write",
            "--add-dir",
            str(root),
            "--output-format",
            "json",
        )
    elif provider is AgentProvider.CODEX:
        runtime_command = ()
        if runtime_options is not None:
            if runtime_options.model is not None:
                runtime_command += ("--model", runtime_options.model)
            if runtime_options.effort is not None:
                effort_value = json.dumps(
                    runtime_options.effort,
                    ensure_ascii=True,
                )
                runtime_command += (
                    "--config",
                    f"model_reasoning_effort={effort_value}",
                )
        command = (
            "codex",
            "exec",
            *runtime_command,
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(root),
            "--ephemeral",
            "--json",
            "-",
        )
    else:
        raise LaunchProfileError(f"unsupported role: {role.value}")

    if permission_profile is PermissionProfile.READ_ONLY:
        writable_roots: tuple[Path, ...] = ()
    elif permission_profile is PermissionProfile.WORKSPACE_WRITE:
        writable_roots = (root,)
    else:
        raise LaunchProfileError(
            f"unsupported permission profile: {permission_profile}"
        )
    if any(path == input_dir.parent for path in writable_roots):
        raise LaunchProfileError(
            "launch profile cannot grant the entire step root"
        )
    return LaunchProfile(
        command=command,
        writable_roots=writable_roots,
    )
