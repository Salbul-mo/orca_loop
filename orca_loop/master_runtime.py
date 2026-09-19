from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from .contracts import ContractViolationError, parse_master_decision
from .models import (
    AgentProvider,
    MasterDecision,
    MasterRuntimeConfig,
    MasterRuntimeOptions,
)


class MasterRuntimeAdapterError(RuntimeError):
    """Raised when the Master runtime boundary cannot produce a valid decision."""


MasterInvoker = Callable[[MasterRuntimeOptions, str], str]
MAX_MASTER_TIMEOUT_MS = 14_400_000


def build_master_command(options: MasterRuntimeOptions) -> tuple[str, ...]:
    """Build a noninteractive, non-writing command for one Master decision."""
    if options.provider is AgentProvider.CLAUDE:
        runtime: tuple[str, ...] = ()
        if options.model is not None:
            runtime += ("--model", options.model)
        if options.effort is not None:
            runtime += ("--effort", options.effort)
        return (
            "claude",
            "-p",
            *runtime,
            "--restricted",
            "--tools",
            "",
            "--permission-mode",
            "plan",
            "--output-format",
            "text",
        )
    if options.provider is AgentProvider.CODEX:
        runtime = ()
        if options.model is not None:
            runtime += ("--model", options.model)
        if options.effort is not None:
            effort_value = json.dumps(options.effort, ensure_ascii=True)
            runtime += (
                "--config",
                f"model_reasoning_effort={effort_value}",
            )
        return (
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            *runtime,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-",
        )
    raise MasterRuntimeAdapterError(
        f"unsupported master provider: {options.provider}"
    )


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            shell=False,
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def invoke_master_provider(
    options: MasterRuntimeOptions,
    prompt: str,
    *,
    timeout_ms: int,
) -> str:
    """Invoke the configured Master without exposing the target repository cwd."""
    if not 1 <= timeout_ms <= MAX_MASTER_TIMEOUT_MS:
        raise MasterRuntimeAdapterError(
            f"timeout_ms must be 1..{MAX_MASTER_TIMEOUT_MS}"
        )
    command = build_master_command(options)
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    with tempfile.TemporaryDirectory(prefix="orca-master-") as directory:
        try:
            process = subprocess.Popen(
                command,
                cwd=Path(directory),
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise MasterRuntimeAdapterError(
                f"failed to start master provider: {command[0]}"
            ) from exc
        try:
            stdout_raw, stderr_raw = process.communicate(
                input=prompt.encode("utf-8"),
                timeout=timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_tree(process)
            process.communicate()
            raise MasterRuntimeAdapterError(
                f"master provider timed out after {timeout_ms} ms"
            ) from exc
    stdout = stdout_raw.decode("utf-8", "replace")
    stderr = stderr_raw.decode("utf-8", "replace")
    if process.returncode != 0:
        raise MasterRuntimeAdapterError(
            f"master provider exited {process.returncode}: {stderr[-4096:]}"
        )
    if not stdout.strip():
        raise MasterRuntimeAdapterError("master provider output must be nonempty text")
    return stdout


def render_master_prompt(
    template_path: Path,
    context: Mapping[str, object],
) -> str:
    path = template_path.resolve()
    if not path.is_file():
        raise MasterRuntimeAdapterError(f"master prompt does not exist: {path}")
    try:
        template = path.read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeDecodeError) as exc:
        raise MasterRuntimeAdapterError(
            f"failed to read master prompt: {path}"
        ) from exc
    if not template:
        raise MasterRuntimeAdapterError("master prompt must be nonempty")
    try:
        context_json = json.dumps(
            dict(context),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MasterRuntimeAdapterError(
            "master decision context must be JSON serializable"
        ) from exc
    return f"{template}\n\n## Decision context\n\n{context_json}\n"


def invoke_master(
    config: MasterRuntimeConfig,
    template_path: Path,
    context: Mapping[str, object],
    invoke: MasterInvoker,
) -> MasterDecision:
    prompt = render_master_prompt(template_path, context)
    try:
        raw = invoke(config.master, prompt)
    except Exception as exc:
        raise MasterRuntimeAdapterError("master provider invocation failed") from exc
    if not isinstance(raw, str) or not raw.strip():
        raise MasterRuntimeAdapterError("master provider output must be nonempty text")
    try:
        return parse_master_decision(raw)
    except ContractViolationError as exc:
        raise MasterRuntimeAdapterError(
            f"master provider returned invalid decision: {exc}"
        ) from exc
