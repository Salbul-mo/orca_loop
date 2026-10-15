"""Explicitly authorized local-bridge adapter for web ChatGPT Orca roles.

The bridge and Chrome extension own conversation creation and attribution. This
program never uses browser cookies or attempts to infer an active conversation.
Its stdin/stdout protocol matches the existing deterministic Orca wrappers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROLES = (
    "master", "planner", "plan_reviewer", "implementer",
    "code_reviewer", "cross_confirmer",
)


class BrowserProviderError(RuntimeError):
    pass


def _endpoint() -> str:
    raw = os.environ.get("ORCA_WEB_AGENT_BRIDGE_URL", "http://127.0.0.1:8765")
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserProviderError("bridge URL must be loopback HTTP with an explicit port")
    return raw.rstrip("/")


def _request(endpoint: str, token: str, method: str, path: str, payload=None) -> dict:
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint + path,
        data=raw,
        method=method,
        headers={
            "x-orca-web-agent-token": token,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        raise BrowserProviderError(
            "bridge_http: rejected request (HTTP " + str(exc.code) + ")"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BrowserProviderError(
            "bridge_transport: browser bridge is unavailable: "
            + type(exc).__name__
            + ": "
            + str(exc)
        ) from exc
    except ValueError as exc:
        raise BrowserProviderError(
            "bridge_protocol: bridge returned invalid JSON: "
            + type(exc).__name__
            + ": "
            + str(exc)
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("job"), dict):
        raise BrowserProviderError(
            "bridge_protocol: response does not contain a job"
        )
    return value["job"]


def invoke(role: str, prompt: str, timeout_seconds: float, *,
           terminal_policy: str | None = None,
           proof_sha256: str | None = None) -> str:
    if role not in ROLES:
        raise BrowserProviderError("unrecognized web agent role")
    if not prompt.strip():
        raise BrowserProviderError("empty agent prompt")
    token = os.environ.get("ORCA_WEB_AGENT_TOKEN", "")
    if len(token) < 32:
        token = os.environ.get("MCP_WEB_AGENT_CONTROL_TOKEN", "")
    if len(token) < 32:
        raise BrowserProviderError(
            "ORCA_WEB_AGENT_TOKEN or inherited MCP web-agent control token "
            "must be available"
        )
    if terminal_policy not in (None, "sentinel_dom", "orca_terminal", "read_tool_probe", "multi_read_probe", "inspect_read_probe"):
        raise BrowserProviderError("unsupported browser terminal policy")
    if terminal_policy in ("read_tool_probe", "multi_read_probe", "inspect_read_probe"):
        import re
        if role != "planner" or not isinstance(proof_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", proof_sha256):
            raise BrowserProviderError("read-only tool verification requires a valid private SHA-256")
    elif proof_sha256 is not None:
        raise BrowserProviderError("SHA-256 proof is reserved for read-only tool verification")
    if terminal_policy == "orca_terminal":
        prompt = prompt.replace(
            "- stdout must begin with `{` and end with `}`.",
            "- Worker stdout after transport framing is removed must begin with `{` and end with `}`.",
        ).replace(
            "Return exactly one JSON object. Never return prose, Markdown, a code fence,",
            "The artifact content returned to the worker must be exactly one JSON object. Never put prose, Markdown, a code fence,",
        ).replace(
            "Return the artifact JSON only on stdout.",
            "Return the artifact JSON only in worker stdout after transport framing is removed.",
        ).replace(
            "Return exactly one JSON object with exactly these fields:",
            "The decision artifact returned after transport framing is removed must be exactly one JSON object with exactly these fields:",
        )
        prompt = (
            prompt.rstrip()
            + "\n\nTRANSPORT COMPATIBILITY NOTE: The browser response is a transport envelope, not worker stdout. The requested artifact itself must obey every JSON-only rule above and end at its closing }. After that artifact closing }, the browser response MUST continue with the exact ORCA_WEB_RESULT_END marker required by the following FINAL OUTPUT PROTOCOL. The bridge strips that marker before producing worker stdout or validating the artifact. A browser response that ends at } without the marker is incomplete and must not be returned."
        )
    endpoint = _endpoint()
    payload = {"role": role, "prompt": prompt}
    if terminal_policy:
        payload["terminalPolicy"] = terminal_policy
    if proof_sha256 is not None:
        payload["proofSha256"] = proof_sha256
    job = _request(endpoint, token, "POST", "/automation/jobs", payload)
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise BrowserProviderError(
            "bridge_protocol: bridge failed to assign a job id"
        )
    # stderr is human-facing diagnostic only; never print the credential/prompt.
    print("web_agent job id: " + job_id, file=sys.stderr, flush=True)
    deadline = time.monotonic() + timeout_seconds
    terminal_state_observed = False
    try:
        while time.monotonic() < deadline:
            job = _request(endpoint, token, "GET", "/automation/jobs/" + job_id)
            state = job.get("state")
            if state == "completed":
                terminal_state_observed = True
                result = job.get("result")
                if not isinstance(result, str) or not result.strip():
                    raise BrowserProviderError(
                        "result_retrieval: completed browser job has no result"
                    )
                return result
            if state in {"failed", "cancelled"}:
                terminal_state_observed = True
                raise BrowserProviderError(
                    "model_or_browser: web agent "
                    + str(state)
                    + ": "
                    + str(job.get("error") or "no details")
                )
            if state not in {"pending", "running"}:
                raise BrowserProviderError(
                    "bridge_protocol: unknown web agent state "
                    + repr(state)
                )
            time.sleep(min(1.5, max(0.05, deadline - time.monotonic())))
        raise BrowserProviderError(
            "provider_timeout: web agent timed out before a terminal result"
        )
    finally:
        if not terminal_state_observed:
            # Best effort: abnormal polling exits must not leave an old browser
            # job running while the coordinator retries with a new one.
            try:
                _request(
                    endpoint,
                    token,
                    "POST",
                    "/automation/jobs/" + job_id + "/cancel",
                    {},
                )
            except BrowserProviderError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=ROLES, required=True)
    # Consumed by dispatcher._profile_working_directory; not a permission grant.
    parser.add_argument("--add-dir", default=None)
    options = parser.parse_args()
    try:
        timeout = float(os.environ.get("ORCA_WEB_AGENT_TIMEOUT_SECONDS", "3600"))
        if not 1 <= timeout <= 14400:
            raise BrowserProviderError("invalid ORCA_WEB_AGENT_TIMEOUT_SECONDS")
        output = invoke(
            options.role,
            sys.stdin.read(),
            timeout,
            terminal_policy="orca_terminal",
        )
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
        return 0
    except BrowserProviderError as exc:
        print("web agent error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
