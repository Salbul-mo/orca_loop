from __future__ import annotations

import json
from collections.abc import Callable

from orca_loop.models import OrcaResponse


# Flags the real Orca CLI accepts, transcribed from its own `validFlags`
# rejection data for version 1.4.180.  Without this the fake accepts argv the
# real binary answers with `invalid_argument`, which is exactly how a
# `gate-create --run` regression reached a fully green suite.
VALID_FLAGS: dict[tuple[str, ...], frozenset[str]] = {
    ("status",): frozenset({"environment", "json", "pairing-code"}),
    ("terminal", "create"): frozenset(
        {"command", "environment", "focus", "json", "pairing-code", "title",
         "worktree"}
    ),
    ("terminal", "send"): frozenset(
        {"enter", "environment", "interrupt", "json", "pairing-code",
         "terminal", "text"}
    ),
    ("terminal", "show"): frozenset(
        {"environment", "json", "pairing-code", "terminal"}
    ),
    ("worktree", "set"): frozenset(
        {"comment", "environment", "json", "pairing-code", "workspace-status",
         "worktree"}
    ),
    ("orchestration", "run-create"): frozenset(
        {"environment", "from", "json", "objective", "pairing-code",
         "retry-request"}
    ),
    ("orchestration", "run-use"): frozenset(
        {"environment", "from", "id", "json", "pairing-code", "retry-request",
         "takeover-legacy"}
    ),
    ("orchestration", "run-current"): frozenset(
        {"environment", "from", "json", "pairing-code"}
    ),
    ("orchestration", "task-create"): frozenset(
        {"deps", "display-name", "environment", "from", "json",
         "pairing-code", "parent", "retry-request", "run", "spec",
         "task-title"}
    ),
    ("orchestration", "dispatch"): frozenset(
        {"dry-run", "environment", "from", "inject", "json", "pairing-code",
         "retry-request", "return-preamble", "run", "task", "to"}
    ),
    ("orchestration", "check"): frozenset(
        {"ack", "all", "environment", "format", "json", "pairing-code",
         "peek", "retry-request", "run", "terminal", "timeout-ms", "types",
         "unread", "wait"}
    ),
    ("orchestration", "send"): frozenset(
        {"body", "dispatch-capability", "dispatch-id", "environment",
         "files-modified", "from", "json", "outcome", "pairing-code",
         "payload", "phase", "priority", "report-path", "retry-request",
         "run", "subject", "task-id", "thread-id", "to", "type"}
    ),
    ("orchestration", "dispatch-show"): frozenset(
        {"environment", "from", "json", "pairing-code", "preamble", "task"}
    ),
    ("orchestration", "worker-list"): frozenset(
        {"environment", "json", "pairing-code", "run", "terminal-state"}
    ),
    ("orchestration", "gate-create"): frozenset(
        {"environment", "from", "json", "options", "pairing-code", "question",
         "retry-request", "task"}
    ),
    ("orchestration", "gate-list"): frozenset(
        {"environment", "from", "json", "pairing-code", "run", "status",
         "task"}
    ),
}


def assert_supported_argv(argv: tuple[str, ...]) -> None:
    """Fail a test whose argv the real Orca CLI would reject."""
    words = tuple(item for item in argv if not item.startswith("--"))
    for length in (2, 1):
        prefix = words[:length]
        if prefix in VALID_FLAGS:
            break
    else:
        raise AssertionError(f"unknown Orca command: {argv!r}")
    allowed = VALID_FLAGS[prefix]
    for item in argv:
        if not item.startswith("--"):
            continue
        name = item[2:]
        if name not in allowed:
            raise AssertionError(
                f"Orca command {' '.join(prefix)} has no flag {item}; "
                f"valid flags are {sorted(allowed)}"
            )
    if prefix == ("orchestration", "send"):
        structured = {
            "--task-id",
            "--dispatch-id",
            "--outcome",
            "--files-modified",
            "--report-path",
            "--phase",
        }
        if "--payload" in argv and any(item in argv for item in structured):
            raise AssertionError(
                "Orca send rejects --payload with structured payload flags"
            )
        if "--type" in argv:
            message_type = argv[argv.index("--type") + 1]
            if message_type == "worker_done" and "--outcome" not in argv:
                raise AssertionError(
                    "Orca worker_done requires the --outcome flag"
                )


def assert_settlement_handshake(send) -> None:
    """Assert the exact two-message settlement a worker must emit.

    Orca allows no custom ``--payload`` on a worker_done, so a successful step
    is an artifact-ready status carrying the digest followed by the worker_done
    that settles the Dispatch — in that order, exactly once each.
    """
    types = [call.kwargs["message_type"] for call in send.call_args_list]
    if types != ["status", "worker_done"]:
        raise AssertionError(
            f"expected ['status', 'worker_done'] settlement, got {types}"
        )
    ready, done = send.call_args_list
    if "artifactDigest" not in ready.kwargs["payload"]:
        raise AssertionError("artifact-ready status carries no digest")
    if done.kwargs["payload"].get("outcome") != "succeeded":
        raise AssertionError("worker_done is not a succeeded settlement")


class FakeOrcaClient:
    def __init__(
        self,
        handler: Callable[[tuple[str, ...], int], dict[str, object]],
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.executable = "C:\\fake\\orca.exe"

    def call(
        self,
        argv: tuple[str, ...],
        *,
        timeout_ms: int,
    ) -> OrcaResponse:
        assert_supported_argv(argv)
        self.calls.append((argv, timeout_ms))
        result = self.handler(argv, timeout_ms)
        wrapper = {"ok": True, "result": result}
        return OrcaResponse(
            result_json=json.dumps(result),
            stdout=json.dumps(wrapper),
            stderr="",
        )
