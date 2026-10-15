"""Run the Orca loop with web ChatGPT for every model role.

By default the deterministic coordinator uses the local transport implemented in
orca_loop.local_runtime, so no external Orca CLI is required. Supplying
--orca-cli explicitly selects the legacy external transport. Browser probes remain
bounded and independent of either coordinator transport.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orca_loop.contracts import (
    ContractViolationError,
    build_agent_runtime_config,
    parse_human_decision,
    parse_master_runtime_config,
)
from orca_loop.models import (
    AgentProvider,
    AgentRuntimeOptions,
    WorkerKey,
)
from orca_loop.generation import GenerationError, load_committed
from orca_loop.local_runtime import LocalCoordinatorClient
from orca_loop.orca_client import OrcaCommandError, OrcaProtocolError
from orca_loop.master_runtime import build_master_command
from tools.web_gpt_provider import BrowserProviderError, invoke

MASTER_CONFIG = ROOT / "configs" / "web-gpt-master.json"
SLOTS = tuple(WorkerKey)
PROBE_TEXT = "ORCA_WEB_GPT_PROBE_OK"


def validate_preset() -> dict[str, object]:
    """Validate both Master and all worker choices without an Orca process."""
    config = parse_master_runtime_config(MASTER_CONFIG.read_text(encoding="utf-8"))
    if config.master.provider is not AgentProvider.WEB_GPT:
        raise ValueError("master preset does not select web_gpt")
    master_command = build_master_command(config.master)
    if not master_command or "--role" not in master_command:
        raise ValueError("web_gpt Master adapter command is missing")
    workers = build_agent_runtime_config(tuple(
        AgentRuntimeOptions(worker, AgentProvider.WEB_GPT, None, None)
        for worker in SLOTS
    ))
    if any(item.provider is not AgentProvider.WEB_GPT for item in workers.agents):
        raise ValueError("not every worker is configured for web_gpt")
    return {
        "status": "PASS",
        "mode": "check-config",
        "master": "web_gpt",
        "workers": {worker.value: "web_gpt" for worker in SLOTS},
    }


def _validated_run_id(run_id: str | None) -> str:
    if not run_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", run_id):
        raise ValueError("a valid --run-id is required for supervision operations")
    return run_id


def supervision_status(run_id: str) -> dict[str, object]:
    run_id = _validated_run_id(run_id)
    control_dir = ROOT / "runs" / run_id / "control"
    state, _ledger, _manifest = load_committed(control_dir)
    binding = state.gate_binding
    if binding is None:
        return {
            "status": "NONE",
            "runId": run_id,
            "state": state.state.value,
        }
    client = LocalCoordinatorClient(
        cwd=ROOT,
        run_id=run_id,
        coordinator_handle=state.coordinator_handle,
    )
    response = client.call(
        (
            "orchestration", "gate-list",
            "--task", binding.task_id,
        ),
        timeout_ms=1000,
    )
    value = json.loads(response.result_json)
    gates = value.get("gates", []) if isinstance(value, dict) else []
    gate = next(
        (
            item for item in gates
            if isinstance(item, dict) and item.get("id") == binding.gate_id
        ),
        None,
    )
    notification_path = client.notification_dir / f"{binding.gate_id}.json"
    notification = None
    if notification_path.is_file():
        parsed = json.loads(notification_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            notification = parsed
    return {
        "status": "PENDING" if isinstance(gate, dict) and gate.get("status") == "pending" else "RESOLVED",
        "runId": run_id,
        "state": state.state.value,
        "blockedFromState": (
            None if state.blocked_from_state is None else state.blocked_from_state.value
        ),
        "gateId": binding.gate_id,
        "gateKind": binding.gate_kind.value,
        "question": None if not isinstance(gate, dict) else gate.get("question"),
        "options": [] if not isinstance(gate, dict) else gate.get("options", []),
        "reportPath": str((ROOT / "runs" / run_id / "user-decision.md").resolve()),
        "notification": notification,
    }


def resolve_supervision(options: argparse.Namespace) -> dict[str, object]:
    run_id = _validated_run_id(options.run_id)
    control_dir = ROOT / "runs" / run_id / "control"
    state, _ledger, _manifest = load_committed(control_dir)
    binding = state.gate_binding
    if binding is None:
        raise ValueError("the run has no active human supervision gate")
    client = LocalCoordinatorClient(
        cwd=ROOT,
        run_id=run_id,
        coordinator_handle=state.coordinator_handle,
    )
    listed = json.loads(
        client.call(
            (
                "orchestration", "gate-list",
                "--task", binding.task_id,
                "--status", "pending",
            ),
            timeout_ms=1000,
        ).result_json
    )
    gates = listed.get("gates", []) if isinstance(listed, dict) else []
    gate = next(
        (
            item for item in gates
            if isinstance(item, dict) and item.get("id") == binding.gate_id
        ),
        None,
    )
    if not isinstance(gate, dict):
        raise ValueError("the bound local supervision gate is not pending")
    gate_options = gate.get("options", [])
    if not isinstance(gate_options, list) or options.supervision_decision not in gate_options:
        raise ValueError(
            "decision is not allowed for this gate; allowed: "
            + ", ".join(str(item) for item in gate_options)
        )
    resolution = {
        "decision": options.supervision_decision,
        "decision_note": options.decision_note,
        "affected_acceptance_criteria": tuple(options.affected_criterion or ()),
        "affected_finding_ids": tuple(options.affected_finding or ()),
        "report_digest": binding.report_digest,
    }
    parse_human_decision(json.dumps(resolution, ensure_ascii=False))
    resolved = client.resolve_gate(
        gate_id=binding.gate_id,
        resolution=resolution,
        expected_report_digest=binding.report_digest,
    )
    return {
        "status": "RESOLVED",
        "runId": run_id,
        "gateId": binding.gate_id,
        "decision": options.supervision_decision,
        "resumeRequired": True,
        "gate": resolved,
    }


def _bridge_supervision_decision(
    run_id: str,
    gate_id: str,
) -> dict[str, object] | None:
    raw_endpoint = os.environ.get(
        "ORCA_WEB_AGENT_BRIDGE_URL",
        "http://127.0.0.1:8765",
    ).strip()
    parsed = urllib.parse.urlparse(raw_endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("supervision decision bridge must be a local HTTP endpoint")

    token = os.environ.get("ORCA_WEB_AGENT_TOKEN", "")
    if len(token) < 32:
        token = os.environ.get("MCP_WEB_AGENT_CONTROL_TOKEN", "")
    if len(token) < 32:
        raise ValueError("supervision decision polling requires the remote-mcp control token")

    url = (
        raw_endpoint.rstrip("/")
        + "/automation/supervision/"
        + urllib.parse.quote(gate_id, safe="")
        + "/decision"
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"x-orca-web-agent-token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            if response.status == 204:
                return None
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            return None
        raise ValueError(
            f"remote-mcp supervision decision endpoint returned HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError:
        return None

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("remote-mcp supervision decision response is malformed") from exc
    if not isinstance(value, dict):
        raise ValueError("remote-mcp supervision decision response must be an object")
    if value.get("runId") != run_id or value.get("gateId") != gate_id:
        raise ValueError("remote-mcp supervision decision provenance mismatch")
    decision = value.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("remote-mcp supervision decision payload is missing")
    kind = decision.get("decision")
    note = decision.get("note")
    if kind not in {"merge", "reject", "revise_code", "revise_design"}:
        raise ValueError("remote-mcp supervision decision is invalid")
    if note is not None and not isinstance(note, str):
        raise ValueError("remote-mcp supervision decision note is invalid")
    return {
        "decision": kind,
        "note": note,
    }


def _resolve_bridge_supervision(
    run_id: str,
    remote: dict[str, object],
) -> dict[str, object]:
    control_dir = ROOT / "runs" / run_id / "control"
    state, ledger, _manifest = load_committed(control_dir)
    binding = state.gate_binding
    if binding is None:
        raise ValueError("the run has no active human supervision gate")

    decision = str(remote["decision"])
    note_value = remote.get("note")
    note = note_value if isinstance(note_value, str) and note_value.strip() else None
    affected: list[str] = []
    if decision in {"revise_code", "revise_design"}:
        affected.extend(
            record.finding.finding_id
            for record in ledger.findings
            if record.status.value != "RESOLVED"
        )
        if not affected:
            affected.extend(
                trigger.code.value
                for trigger in state.pending_escalations
            )
        if not affected:
            affected.append("human-supervision")

    options = argparse.Namespace(
        run_id=run_id,
        supervision_decision=decision,
        decision_note=note,
        affected_criterion=[],
        affected_finding=affected,
    )
    return resolve_supervision(options)


def _wait_for_bridge_supervision(
    run_id: str,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    announced_gate: str | None = None
    first_poll = True
    while first_poll or time.monotonic() < deadline:
        first_poll = False
        state, _ledger, _manifest = load_committed(
            ROOT / "runs" / run_id / "control"
        )
        binding = state.gate_binding
        if binding is None:
            return False
        if binding.gate_id != announced_gate:
            print(
                "[ORCA] 사람 감독 답변을 기다립니다. "
                "Chrome 알림을 눌러 질문 창에서 결정해 주세요.",
                file=sys.stderr,
                flush=True,
            )
            announced_gate = binding.gate_id
        remote = _bridge_supervision_decision(run_id, binding.gate_id)
        if remote is not None:
            resolved = _resolve_bridge_supervision(run_id, remote)
            print(
                json.dumps(
                    {
                        "status": "SUPERVISION_ANSWER_RECEIVED",
                        "runId": run_id,
                        "gateId": binding.gate_id,
                        "decision": resolved["decision"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            return True
        if timeout_seconds <= 0:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    return False


def web_loop_argv(options: argparse.Namespace) -> list[str]:
    """Build ordinary run_loop.py arguments; never bypass its preflight."""
    required = ("request", "worktree")
    missing = [key for key in required if not getattr(options, key, None)]
    if missing:
        raise ValueError("missing required option(s): " + ", ".join(missing))
    request = Path(options.request).resolve()
    worktree = Path(options.worktree).resolve()
    if not request.is_file():
        raise ValueError("request file does not exist")
    if not worktree.is_dir():
        raise ValueError("target Git directory does not exist")
    run_id = options.run_id or ("web-gpt-" + uuid.uuid4().hex[:12])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", run_id):
        raise ValueError("invalid run-id")
    argv = [
        "--run-id", run_id,
        "--request", str(request),
        "--worktree", str(worktree),
        "--coordinator-handle", options.coordinator_handle or "local-coordinator",
        "--master-config", str(MASTER_CONFIG),
    ]
    for key in SLOTS:
        argv.extend(("--agent-provider", key.value + "=web_gpt"))
    if options.test_policy:
        policy = Path(options.test_policy).resolve()
        if not policy.is_file():
            raise ValueError("test policy does not exist")
        argv.extend(("--test-policy", str(policy)))
    if getattr(options, "master_decision", None):
        argv.extend(("--master-decision", options.master_decision))
        if getattr(options, "master_role", None):
            argv.extend(("--master-role", options.master_role))
        if getattr(options, "master_permission_profile", None):
            argv.extend((
                "--master-permission-profile",
                options.master_permission_profile,
            ))
        if getattr(options, "master_reason", None):
            argv.extend(("--master-reason", options.master_reason))
    if options.planning_only:
        argv.append("--planning-only")
    if options.resume:
        argv.append("--resume")
    if options.dry_run:
        argv.append("--dry-run")
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Orca with web ChatGPT for the Master and all workers.",
    )
    parser.add_argument("--check-config", action="store_true",
                        help="Validate all web_gpt roles locally; no Orca, browser or secret.")
    parser.add_argument("--probe", action="store_true",
                        help="Submit one harmless REAL browser task; requires bridge token.")
    parser.add_argument("--probe-general", action="store_true",
                        help="Test a variable-answer, no-tools browser task with explicit terminal marker.")
    parser.add_argument("--probe-format", action="store_true",
                        help="Test actual multi-paragraph formatting across browser and Bridge.")
    parser.add_argument("--probe-read-tool", action="store_true",
                        help="Verify a real read-only browser MCP tool call using an unpredictable temporary-file token.")
    parser.add_argument("--probe-multi-read-tool", action="store_true",
                        help="Verify list_directory + read_file in one browser Worker using server-side witnesses.")
    parser.add_argument("--probe-inspect-read-tool", action="store_true",
                        help="Verify list_directory + stat_path + read_file in one browser Worker using ordered server-side witnesses.")
    parser.add_argument("--request", help="Existing UTF-8 task description file.")
    parser.add_argument("--worktree", help="Existing clean Git repository to work on.")
    parser.add_argument("--coordinator-handle",
                        help="Coordinator handle for legacy --orca-cli mode; local mode uses local-coordinator.")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--supervision-status",
        action="store_true",
        help="Show the active local human-supervision gate for --run-id.",
    )
    parser.add_argument(
        "--supervision-decision",
        choices=("merge", "reject", "revise_code", "revise_design"),
        help="Resolve the active local human-supervision gate for --run-id.",
    )
    parser.add_argument("--decision-note")
    parser.add_argument("--affected-criterion", action="append", default=[])
    parser.add_argument("--affected-finding", action="append", default=[])
    parser.add_argument("--test-policy")
    parser.add_argument("--orca-cli",
                        help="Path to actual Orca CLI binary (not the graphical desktop app).")
    parser.add_argument(
        "--master-decision",
        choices=("dispatch", "test", "finish", "escalate", "abort"),
        help="Inject one validated Master decision while resuming.",
    )
    parser.add_argument(
        "--master-role",
        choices=(
            "planner",
            "plan_reviewer",
            "implementer",
            "code_reviewer",
            "cross_confirmer",
        ),
    )
    parser.add_argument(
        "--master-permission-profile",
        choices=("read_only", "workspace_write"),
    )
    parser.add_argument("--master-reason")
    parser.add_argument(
        "--planning-only",
        action="store_true",
        help="Stop after approved plan consensus; do not enter implementation.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--supervision-wait-seconds",
        type=float,
        default=3600.0,
        help="How long local Web GPT runs wait for a question-window answer before returning BLOCKED; 0 disables waiting.",
    )
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    try:
        check = validate_preset()
        if options.check_config:
            print(json.dumps(check, sort_keys=True))
            return 0
        if options.supervision_status:
            if options.orca_cli:
                raise ValueError("--supervision-status is only supported in local runtime mode")
            print(
                json.dumps(
                    supervision_status(options.run_id),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if options.supervision_decision:
            if options.orca_cli:
                raise ValueError("--supervision-decision is only supported in local runtime mode")
            resolved = resolve_supervision(options)
            print(json.dumps(resolved, ensure_ascii=False, sort_keys=True))
            if not options.resume:
                return 0
        if options.probe_inspect_read_tool:
            secret = secrets.token_hex(24)
            with tempfile.TemporaryDirectory(prefix="orca-web-inspect-read-probe-") as temp:
                probe_root = Path(temp)
                probe_path = probe_root / "read-only-token.txt"
                probe_path.write_text(secret, encoding="ascii")
                try:
                    answer = invoke(
                        "planner",
                        "Read-only inspection worker check. First use the connected remote-mcp "
                        "list_directory tool on the exact temporary directory at: " + str(probe_root) +
                        "\nThen use the connected remote-mcp stat_path tool on the exact temporary "
                        "file at: " + str(probe_path) +
                        "\nThen use the connected remote-mcp read_file tool to read the exact "
                        "temporary file at: " + str(probe_path) +
                        "\nDo not guess the file content. The token is deliberately absent from "
                        "this prompt. You must use all three tools in that order. Return only the exact "
                        "token from the file, followed by the separate job-specific final marker "
                        "defined below. If any remote-mcp tool is unavailable, write only "
                        "TOOL_UNAVAILABLE before the final marker. Do not modify any files.",
                        180.0,
                        terminal_policy="inspect_read_probe",
                        proof_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
                    )
                    if answer != secret:
                        raise BrowserProviderError(
                            "browser returned an incorrect inspect-read temporary file token"
                        )
                finally:
                    secret = ""
            print(json.dumps({"status": "PASS", "mode": "real-inspect-read-only-mcp-tools"}))
            return 0
        if options.probe_multi_read_tool:
            secret = secrets.token_hex(24)
            with tempfile.TemporaryDirectory(prefix="orca-web-multi-read-probe-") as temp:
                probe_root = Path(temp)
                probe_path = probe_root / "read-only-token.txt"
                probe_path.write_text(secret, encoding="ascii")
                try:
                    answer = invoke(
                        "planner",
                        "Read-only multi-tool worker check. First use the connected remote-mcp "
                        "list_directory tool on the exact temporary directory at: " + str(probe_root) +
                        "\nThen use the connected remote-mcp read_file tool to read the exact "
                        "temporary file at: " + str(probe_path) +
                        "\nDo not guess the file content. The token is deliberately absent from "
                        "this prompt. You must use both tools in that order. Return only the exact "
                        "token from the file, followed by the separate job-specific final marker "
                        "defined below. If either remote-mcp tool is unavailable, write only "
                        "TOOL_UNAVAILABLE before the final marker. Do not modify any files.",
                        180.0,
                        terminal_policy="multi_read_probe",
                        proof_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
                    )
                    if answer != secret:
                        raise BrowserProviderError(
                            "browser returned an incorrect multi-read temporary file token"
                        )
                finally:
                    secret = ""
            print(json.dumps({"status": "PASS", "mode": "real-multi-read-only-mcp-tools"}))
            return 0
        if options.probe_read_tool:
            # The secret is NEVER included in the browser prompt. Producing it
            # requires actually reading this unique temporary file through a
            # filesystem-access tool on the connected Windows host.
            secret = secrets.token_hex(24)
            with tempfile.TemporaryDirectory(prefix="orca-web-read-probe-") as temp:
                probe_path = Path(temp) / "read-only-token.txt"
                probe_path.write_text(secret, encoding="ascii")
                try:
                    answer = invoke(
                        "planner",
                        "Read-only tool worker check. Use the connected remote-mcp read_file "
                        "tool to read the exact temporary file at: " + str(probe_path) +
                        "\nDo not guess the file content. The token is deliberately absent "
                        "from this prompt. Return only the exact token in the file, "
                        "followed by the separate job-specific final marker defined below. "
                        "If the remote-mcp read_file tool is not accessible, write only "
                        "TOOL_UNAVAILABLE before the final marker. Do not modify any files.",
                        180.0,
                        terminal_policy="read_tool_probe",
                        proof_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
                    )
                    if answer != secret:
                        raise BrowserProviderError(
                            "browser returned an incorrect temporary file token"
                        )
                finally:
                    # TemporaryDirectory deletes the fixture on both success
                    # and failure. Never print or log the random token.
                    secret = ""
            print(json.dumps({"status": "PASS", "mode": "real-read-only-mcp-tool"}))
            return 0
        if options.probe_format:
            # Require semantic paragraph separators in the actual returned text,
            # not merely a token-concatenated response accepted by the marker gate.
            nonce = secrets.token_hex(4).upper()
            lines = tuple(f"{label}_{nonce}" for label in ("FIRST", "SECOND", "THIRD"))
            answer = invoke(
                "planner",
                "Read-only text formatting test. Output precisely the three "
                "following standalone paragraphs, separated by one completely "
                "blank line, without heading, bullets, quotes or code fences:"
                "\n\n" + "\n\n".join(lines) +
                "\n\nAfter the three paragraphs append the protocol's "
                "job-specific final marker as its own final line. "
                "Do not call tools or modify files.",
                180.0,
                terminal_policy="sentinel_dom",
            )
            expected = "\n\n".join(lines)
            actual = answer.strip().replace("\r\n", "\n")
            if actual != expected:
                raise BrowserProviderError(
                    "browser answer did not preserve exact paragraph boundaries"
                )
            print(json.dumps({"status": "PASS", "mode": "multiline-no-tools"}))
            return 0
        if options.probe_general:
            left = 20 + secrets.randbelow(70)
            right = 20 + secrets.randbelow(70)
            answer = invoke(
                "planner",
                f"Read-only general worker check. Compute {left} + {right}. "
                "Answer only with the integer, followed by the separate final marker "
                "specified in the output protocol. Do not call tools or modify files.",
                180.0,
                terminal_policy="sentinel_dom",
            )
            if answer.strip() != str(left + right):
                raise BrowserProviderError("generic worker output failed variable-answer verification")
            print(json.dumps({"status": "PASS", "mode": "generic-no-tools"}))
            return 0
        if options.probe:
            answer = invoke(
                "planner",
                "Read-only browser connectivity test. Reply with exactly " +
                PROBE_TEXT + ". Do not call any tools or modify files.",
                120.0,
            )
            if answer.strip() != PROBE_TEXT:
                raise BrowserProviderError(
                    "browser replied, but the answer did not match the probe marker"
                )
            print(json.dumps({"status": "PASS", "mode": "live-probe"}))
            return 0
        args = web_loop_argv(options)
        explicit_token = os.environ.get("ORCA_WEB_AGENT_TOKEN", "")
        inherited_token = os.environ.get("MCP_WEB_AGENT_CONTROL_TOKEN", "")
        if not options.dry_run and max(len(explicit_token), len(inherited_token)) < 32:
            raise ValueError(
                "set ORCA_WEB_AGENT_TOKEN or run from an authenticated remote-mcp "
                "environment before executing a live loop"
            )
        previous_runtime = os.environ.get("ORCA_LOOP_RUNTIME")
        previous_cli = os.environ.get("ORCA_CLI_COMMAND")
        try:
            if options.orca_cli:
                if not options.coordinator_handle:
                    raise ValueError(
                        "--coordinator-handle is required with explicit --orca-cli"
                    )
                cli = Path(options.orca_cli).resolve()
                if not cli.is_file():
                    raise ValueError("--orca-cli path does not exist")
                os.environ.pop("ORCA_LOOP_RUNTIME", None)
                os.environ["ORCA_CLI_COMMAND"] = str(cli)
            else:
                os.environ["ORCA_LOOP_RUNTIME"] = "local"
                os.environ.pop("ORCA_CLI_COMMAND", None)
            # The state machine and all validation remain unchanged. Only the
            # terminal/task/message transport is local in the default Web GPT mode.
            from run_loop import EXIT_USER_REQUIRED, main as run_main
            result = run_main(args)
            if (
                options.orca_cli
                or options.dry_run
            ):
                return result
            run_id = args[args.index("--run-id") + 1]
            resume_args = args if "--resume" in args else [*args, "--resume"]
            while result == EXIT_USER_REQUIRED:
                if not _wait_for_bridge_supervision(
                    run_id,
                    options.supervision_wait_seconds,
                ):
                    return result
                result = run_main(resume_args)
            return result
        finally:
            if previous_runtime is None:
                os.environ.pop("ORCA_LOOP_RUNTIME", None)
            else:
                os.environ["ORCA_LOOP_RUNTIME"] = previous_runtime
            if previous_cli is None:
                os.environ.pop("ORCA_CLI_COMMAND", None)
            else:
                os.environ["ORCA_CLI_COMMAND"] = previous_cli
    except (
        ValueError,
        OSError,
        BrowserProviderError,
        ContractViolationError,
        GenerationError,
        OrcaCommandError,
        OrcaProtocolError,
    ) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
