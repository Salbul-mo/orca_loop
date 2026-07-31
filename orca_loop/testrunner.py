from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .models import (
    PolicyValidation,
    SnapshotIdentity,
    TestCommand,
    TestCommandResult,
    TestExecutionPolicy,
    TestFailureAttribution,
    TestGateResult,
    TestGateStatus,
    TestPolicyViolation,
)
from .snapshot import capture_snapshot


MAX_COMMAND_TIMEOUT_MS = 3_600_000
MAX_OUTPUT_TAIL_BYTES = 32 * 1024
BASE_ENV_KEYS = (
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "TEMP",
    "TMP",
)


class TestPolicyError(ValueError):
    """Raised when the exact test execution policy is invalid."""


class TestExecutionError(RuntimeError):
    """Raised when an allowlisted test process cannot be started."""


def _relative_path(value: str, worktree: Path) -> Path | None:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (worktree / candidate).resolve()
    try:
        resolved.relative_to(worktree)
    except ValueError:
        return None
    return resolved


def validate_test_commands(
    commands: tuple[TestCommand, ...],
    policy: TestExecutionPolicy,
    worktree: Path,
) -> PolicyValidation:
    root = worktree.resolve()
    violations: list[TestPolicyViolation] = []
    allowed = set(policy.allowed_commands)
    for index, command in enumerate(commands):
        if command not in allowed:
            violations.append(
                TestPolicyViolation(
                    "command_not_allowlisted",
                    index,
                    "command must exactly equal one policy entry",
                )
            )
        if _relative_path(command.cwd, root) is None:
            violations.append(
                TestPolicyViolation(
                    "cwd_outside_worktree",
                    index,
                    f"invalid command cwd: {command.cwd!r}",
                )
            )
        if command.timeout_ms > MAX_COMMAND_TIMEOUT_MS:
            violations.append(
                TestPolicyViolation(
                    "timeout_too_large",
                    index,
                    f"timeout exceeds {MAX_COMMAND_TIMEOUT_MS} ms",
                )
            )
        if command.kind not in policy.approved_kinds:
            violations.append(
                TestPolicyViolation(
                    "kind_not_approved",
                    index,
                    f"test kind {command.kind.value!r} is not approved",
                )
            )
    for output_path in policy.allowed_output_paths:
        if _relative_path(output_path, root) is None:
            violations.append(
                TestPolicyViolation(
                    "output_path_outside_worktree",
                    None,
                    f"invalid allowed output path: {output_path!r}",
                )
            )
    return PolicyValidation(not violations, tuple(violations))


def _sanitized_env(policy: TestExecutionPolicy) -> dict[str, str]:
    allowed = set(BASE_ENV_KEYS) | set(policy.allowed_env_keys)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed
    }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            (
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ),
            shell=False,
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _tail(value: bytes) -> str:
    return value[-MAX_OUTPUT_TAIL_BYTES:].decode("utf-8", "replace")


def _run_command(
    command: TestCommand,
    worktree: Path,
    env: dict[str, str],
) -> TestCommandResult:
    cwd = _relative_path(command.cwd, worktree)
    if cwd is None:
        raise TestExecutionError(
            f"validated command cwd became invalid: {command.cwd}"
        )
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            command.argv,
            cwd=cwd,
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        raise TestExecutionError(
            f"failed to start test command {command.argv!r}: {exc}"
        ) from exc
    try:
        stdout, stderr = process.communicate(
            timeout=command.timeout_ms / 1000
        )
        return TestCommandResult(
            command=command,
            return_code=process.returncode,
            timed_out=False,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
        )
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        return TestCommandResult(
            command=command,
            return_code=None,
            timed_out=True,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
        )


def _allowed_output(
    path: str,
    allowed_paths: tuple[str, ...],
) -> bool:
    candidate = Path(path)
    return any(
        candidate == Path(allowed)
        or Path(allowed) in candidate.parents
        for allowed in allowed_paths
    )


def _snapshot_violations(
    before: SnapshotIdentity,
    after: SnapshotIdentity,
    allowed_output_paths: tuple[str, ...],
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []
    if before.base_head != after.base_head:
        violations.append(
            TestPolicyViolation(
                "head_changed",
                None,
                "test command changed Git HEAD",
            )
        )
    if before.tracked_diff_digest != after.tracked_diff_digest:
        violations.append(
            TestPolicyViolation(
                "tracked_source_changed",
                None,
                "tracked working-tree delta changed during tests",
            )
        )
    if before.staged_diff_digest != after.staged_diff_digest:
        violations.append(
            TestPolicyViolation(
                "staged_source_changed",
                None,
                "staged delta changed during tests",
            )
        )
    before_untracked = dict(before.untracked)
    after_untracked = dict(after.untracked)
    changed_untracked = {
        path
        for path in set(before_untracked) | set(after_untracked)
        if before_untracked.get(path) != after_untracked.get(path)
    }
    outside = sorted(
        path
        for path in changed_untracked
        if not _allowed_output(path, allowed_output_paths)
    )
    if outside:
        violations.append(
            TestPolicyViolation(
                "unapproved_test_output",
                None,
                f"unapproved output paths changed: {outside}",
            )
        )
    return tuple(violations)


def run_tests(
    commands: tuple[TestCommand, ...],
    policy: TestExecutionPolicy,
    worktree: Path,
) -> TestGateResult:
    root = worktree.resolve()
    before = capture_snapshot(root)
    if not commands:
        return TestGateResult(
            status=TestGateStatus.NOT_RUN,
            command_results=(),
            policy_violations=(),
            policy_digest=policy.policy_digest,
            before_snapshot=before,
            after_snapshot=None,
            attribution=TestFailureAttribution.NONE,
        )
    validation = validate_test_commands(commands, policy, root)
    if not validation.approved:
        return TestGateResult(
            status=TestGateStatus.POLICY_VIOLATION,
            command_results=(),
            policy_violations=validation.violations,
            policy_digest=policy.policy_digest,
            before_snapshot=before,
            after_snapshot=None,
            attribution=TestFailureAttribution.AMBIGUOUS,
        )
    env = _sanitized_env(policy)
    results = tuple(
        _run_command(command, root, env)
        for command in commands
    )
    after = capture_snapshot(root)
    delta_violations = _snapshot_violations(
        before,
        after,
        policy.allowed_output_paths,
    )
    if delta_violations:
        status = TestGateStatus.POLICY_VIOLATION
        attribution = TestFailureAttribution.AMBIGUOUS
    elif any(item.timed_out for item in results):
        status = TestGateStatus.FAIL
        attribution = TestFailureAttribution.AMBIGUOUS
    elif any(item.return_code != 0 for item in results):
        status = TestGateStatus.FAIL
        attribution = TestFailureAttribution.IMPLEMENTATION
    else:
        status = TestGateStatus.PASS
        attribution = TestFailureAttribution.NONE
    return TestGateResult(
        status=status,
        command_results=results,
        policy_violations=delta_violations,
        policy_digest=policy.policy_digest,
        before_snapshot=before,
        after_snapshot=after,
        attribution=attribution,
    )
