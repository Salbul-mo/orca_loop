from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Mapping

from .contracts import canonical_json_bytes
from .models import OrcaResponse


MAX_ORCA_TIMEOUT_MS = 14_400_000


class OrcaCommandError(RuntimeError):
    """Raised when the Orca process cannot execute successfully."""


class OrcaTimeoutError(OrcaCommandError):
    """Raised when an Orca command exceeds its bounded timeout."""


class OrcaProtocolError(OrcaCommandError):
    """Raised when Orca output is not the required JSON envelope."""


def resolve_orca_executable(
    environment: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    configured = env.get("ORCA_CLI_COMMAND")
    if configured:
        candidate = configured
    elif env.get("ORCA_DEV_REPO_ROOT"):
        candidate = "orca-dev"
    elif platform.system() == "Linux":
        candidate = "orca-ide"
    else:
        candidate = "orca"
    if Path(candidate).is_absolute():
        if not Path(candidate).is_file():
            raise OrcaCommandError(
                f"selected Orca executable does not exist: {candidate}"
            )
        return str(Path(candidate).resolve())
    resolved = shutil.which(candidate)
    if resolved is None:
        raise OrcaCommandError(
            f"selected Orca executable is not on PATH: {candidate}"
        )
    return resolved


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
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


class OrcaClient:
    def __init__(
        self,
        executable: str | None = None,
        *,
        cwd: Path | None = None,
    ) -> None:
        selected = executable or resolve_orca_executable()
        path = Path(selected)
        if path.is_absolute():
            if not path.is_file():
                raise OrcaCommandError(
                    f"Orca executable does not exist: {path}"
                )
            self._executable = str(path.resolve())
        else:
            resolved = shutil.which(selected)
            if resolved is None:
                raise OrcaCommandError(
                    f"Orca executable is not on PATH: {selected}"
                )
            self._executable = resolved
        self._cwd = None if cwd is None else cwd.resolve()

    @property
    def executable(self) -> str:
        return self._executable

    def call(
        self,
        argv: tuple[str, ...],
        *,
        timeout_ms: int,
    ) -> OrcaResponse:
        if not argv or any(not item for item in argv):
            raise OrcaCommandError("Orca argv must be nonempty strings")
        if not 1 <= timeout_ms <= MAX_ORCA_TIMEOUT_MS:
            raise OrcaCommandError(
                f"timeout_ms must be 1..{MAX_ORCA_TIMEOUT_MS}"
            )
        command = (
            self._executable,
            *argv,
            *(("--json",) if "--json" not in argv else ()),
        )
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=self._cwd,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise OrcaCommandError(
                f"failed to start Orca command {command!r}: {exc}"
            ) from exc
        try:
            stdout_raw, stderr_raw = process.communicate(
                timeout=timeout_ms / 1000
            )
        except subprocess.TimeoutExpired as exc:
            _terminate_tree(process)
            process.communicate()
            raise OrcaTimeoutError(
                f"Orca command timed out after {timeout_ms} ms: {argv!r}"
            ) from exc
        stdout = stdout_raw.decode("utf-8", "replace")
        stderr = stderr_raw.decode("utf-8", "replace")
        if process.returncode != 0:
            raise OrcaCommandError(
                f"Orca command failed ({process.returncode}): {argv!r}; "
                f"stderr={stderr[-4096:]!r}; stdout={stdout[-4096:]!r}"
            )
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OrcaProtocolError(
                f"Orca stdout is not one JSON document: {stdout[-4096:]!r}"
            ) from exc
        if not isinstance(value, dict):
            raise OrcaProtocolError(
                "Orca response root must be an object"
            )
        if value.get("ok") is False:
            raise OrcaProtocolError(
                f"Orca returned ok=false: {value.get('error')!r}"
            )
        result = value.get("result")
        return OrcaResponse(
            result_json=canonical_json_bytes(result).decode("utf-8"),
            stdout=stdout,
            stderr=stderr,
        )
