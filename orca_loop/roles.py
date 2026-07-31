from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .contracts import to_wire_value
from .models import RenderedContract, Role, RoleContext, TestGateStatus


MAX_RENDERED_CONTRACT_BYTES = 256 * 1024
PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
BASE_PLACEHOLDERS = {
    "ROLE",
    "RUN_ID",
    "CONSENSUS_ROUND",
    "WORKTREE_PATH",
    "STEP_INPUT_DIR",
    "STEP_OUTPUT_DIR",
    "COORDINATOR_HANDLE",
    "PLAN_VERSION",
    "SNAPSHOT_DIGEST",
    "SCOPE_PACKAGE_JSON",
    "DELIVERED_FINDING_IDS",
    "ARTIFACT_FILE",
}
POLICY_PLACEHOLDERS = {
    "ALLOWED_TEST_COMMANDS",
    "TEST_POLICY_DIGEST",
    "APPROVED_TEST_KINDS",
    "ALLOWED_TEST_OUTPUT_PATHS",
}
ALLOWED_PLACEHOLDERS = {
    Role.PLANNER: BASE_PLACEHOLDERS | POLICY_PLACEHOLDERS,
    Role.PLAN_REVIEWER: BASE_PLACEHOLDERS | POLICY_PLACEHOLDERS,
    Role.IMPLEMENTER: BASE_PLACEHOLDERS | {"TEST_GATE_RESULT"},
    Role.CODE_REVIEWER: BASE_PLACEHOLDERS | {"TEST_GATE_RESULT"},
    Role.CROSS_CONFIRMER: BASE_PLACEHOLDERS | {"TEST_GATE_RESULT"},
}
ARTIFACT_FILENAMES = {
    Role.PLANNER: "plan.json",
    Role.PLAN_REVIEWER: "plan-review.json",
    Role.IMPLEMENTER: "implementation.json",
    Role.CODE_REVIEWER: "code-review.json",
    Role.CROSS_CONFIRMER: "cross-review.json",
}


class TemplateContractError(ValueError):
    """Raised when a role template is incomplete or unbounded."""


def _compact(value: object) -> str:
    return json.dumps(
        to_wire_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _mapping(context: RoleContext) -> dict[str, str]:
    if context.scope_package.finding_ids != context.delivered_finding_ids:
        raise TemplateContractError(
            "delivered_finding_ids must equal unresolved scope finding_ids"
        )
    if context.role in {
        Role.CODE_REVIEWER,
        Role.CROSS_CONFIRMER,
    } and context.test_gate_result not in {
        TestGateStatus.PASS,
        TestGateStatus.NOT_RUN,
    }:
        raise TemplateContractError(
            "code review roles require PASS or NOT_RUN test gate"
        )
    policy = context.test_policy
    mapping = {
        "ROLE": context.role.value,
        "RUN_ID": context.run_id,
        "CONSENSUS_ROUND": str(context.consensus_round),
        "WORKTREE_PATH": str(context.worktree_path.resolve()),
        "STEP_INPUT_DIR": str((context.step_dir / "in").resolve()),
        "STEP_OUTPUT_DIR": str((context.step_dir / "out").resolve()),
        "COORDINATOR_HANDLE": context.coordinator_handle,
        "PLAN_VERSION": str(context.plan_version),
        "SNAPSHOT_DIGEST": context.snapshot_digest,
        "SCOPE_PACKAGE_JSON": _compact(context.scope_package),
        "DELIVERED_FINDING_IDS": _compact(
            context.delivered_finding_ids
        ),
        "TEST_GATE_RESULT": (
            "null"
            if context.test_gate_result is None
            else context.test_gate_result.value
        ),
        "ARTIFACT_FILE": ARTIFACT_FILENAMES[context.role],
        "ALLOWED_TEST_COMMANDS": (
            "[]" if policy is None else _compact(policy.allowed_commands)
        ),
        "TEST_POLICY_DIGEST": (
            "none" if policy is None else policy.policy_digest
        ),
        "APPROVED_TEST_KINDS": (
            "[]" if policy is None else _compact(policy.approved_kinds)
        ),
        "ALLOWED_TEST_OUTPUT_PATHS": (
            "[]" if policy is None else _compact(policy.allowed_output_paths)
        ),
    }
    if context.role in {Role.PLANNER, Role.PLAN_REVIEWER} and policy is None:
        raise TemplateContractError(
            "planner roles require coordinator-owned test policy"
        )
    return mapping


def render_role_contract(
    context: RoleContext,
    template_path: Path,
) -> RenderedContract:
    path = template_path.resolve()
    if not path.is_file():
        raise TemplateContractError(f"template does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    found = set(PLACEHOLDER_PATTERN.findall(text))
    allowed = ALLOWED_PLACEHOLDERS[context.role]
    unknown = found - allowed
    if unknown:
        raise TemplateContractError(
            f"template has unknown placeholders: {sorted(unknown)}"
        )
    missing = allowed - found
    if missing:
        raise TemplateContractError(
            f"template is missing placeholders: {sorted(missing)}"
        )
    mapping = _mapping(context)
    rendered = text
    for name in sorted(found):
        rendered = rendered.replace(f"{{{{{name}}}}}", mapping[name])
    unresolved = PLACEHOLDER_PATTERN.findall(rendered)
    if unresolved:
        raise TemplateContractError(
            f"unresolved placeholders: {sorted(set(unresolved))}"
        )
    raw = rendered.encode("utf-8")
    if len(raw) > MAX_RENDERED_CONTRACT_BYTES:
        raise TemplateContractError(
            f"rendered contract exceeds {MAX_RENDERED_CONTRACT_BYTES} bytes"
        )
    return RenderedContract(
        text=rendered,
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
