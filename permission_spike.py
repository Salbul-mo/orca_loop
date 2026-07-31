from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Sequence


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MARKER_NAME = ".orca-permission-fixture"
MANIFEST_NAME = "permission-fixture-manifest.json"
REPORT_NAME = "permission-feasibility.json"
SOURCE_NAME = "source.txt"
IMPLEMENTATION_TARGET_NAME = "implementation_target.txt"
IMPLEMENTATION_EXPECTED = "approved implementer write\n"
READ_ONLY_ROLES = (
    "claude_planner",
    "claude_code_review",
    "codex_review",
)
ALL_ROLES = (*READ_ONLY_ROLES, "codex_implementer")
CHECK_IDS = (
    "V-PERM-01",
    "V-PERM-02",
    "V-PERM-03",
    "V-PERM-04",
    "V-PERM-05",
)


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class PermissionStrategy(StrEnum):
    ADD_DIR = "A"
    COORDINATOR_STDOUT = "B"
    ARTIFACT_HELPER = "C"
    READONLY_REPOSITORY = "D"


@dataclass(frozen=True)
class PermissionCheck:
    check_id: str
    status: ValidationStatus
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PermissionFeasibilityReport:
    schema_version: int
    run_id: str
    status: ValidationStatus
    strategy: PermissionStrategy | None
    checks: tuple[PermissionCheck, ...]
    evidence: tuple[str, ...]
    orca_version: str
    canonical_path: str
    report_digest: str


@dataclass(frozen=True)
class FixtureManifest:
    schema_version: int
    run_id: str
    fixture_path: str
    source_digest: str
    implementation_target_digest: str
    role_output_paths: tuple[tuple[str, str], ...]


class PermissionSpikeError(RuntimeError):
    """Raised when the live permission feasibility gate cannot complete."""


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value) + b"\n")
    temporary.replace(path)


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def validate_run_id(run_id: str) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PermissionSpikeError(
            "run_id must match [A-Za-z0-9_-]{1,80}"
        )


def run_root(harness_root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    root = (harness_root.resolve() / "runs" / run_id).resolve()
    if not is_within(root, harness_root.resolve() / "runs"):
        raise PermissionSpikeError("run path escaped the harness runs directory")
    return root


def expected_fixture_path(harness_root: Path, run_id: str) -> Path:
    return run_root(harness_root, run_id) / "permission-fixture"


def run_command(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        tuple(argv),
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise PermissionSpikeError(
            f"command failed ({completed.returncode}): {tuple(argv)!r}; "
            f"stderr={completed.stderr[-2048:]!r}"
        )
    return completed


def create_fixture(harness_root: Path, run_id: str) -> FixtureManifest:
    root = run_root(harness_root, run_id)
    fixture = expected_fixture_path(harness_root, run_id)
    if root.exists() and any(root.iterdir()):
        raise PermissionSpikeError(
            f"run directory is not empty: {root}"
        )

    control = root / "control"
    steps = root / "permission-steps"
    fixture.mkdir(parents=True)
    control.mkdir(parents=True)
    steps.mkdir(parents=True)

    (fixture / MARKER_NAME).write_text(
        f"disposable permission fixture for {run_id}\n",
        encoding="utf-8",
        newline="\n",
    )
    source = fixture / SOURCE_NAME
    implementation_target = fixture / IMPLEMENTATION_TARGET_NAME
    source.write_text(
        "permission spike source baseline\n",
        encoding="utf-8",
        newline="\n",
    )
    implementation_target.write_text(
        "implementation baseline\n",
        encoding="utf-8",
        newline="\n",
    )

    role_outputs: list[tuple[str, str]] = []
    for role in ALL_ROLES:
        output = steps / role / "out"
        output.mkdir(parents=True)
        role_outputs.append(
            (role, str((output / "result.json").resolve()))
        )

    run_command(("git", "init"), fixture)
    run_command(("git", "config", "user.name", "orca-permission-spike"), fixture)
    run_command(
        ("git", "config", "user.email", "orca-permission-spike@invalid.local"),
        fixture,
    )
    run_command(("git", "add", "--", "."), fixture)
    run_command(("git", "commit", "-m", "permission spike baseline"), fixture)

    manifest = FixtureManifest(
        schema_version=1,
        run_id=run_id,
        fixture_path=str(fixture),
        source_digest=sha256_file(source),
        implementation_target_digest=sha256_file(implementation_target),
        role_output_paths=tuple(role_outputs),
    )
    write_json_atomic(control / MANIFEST_NAME, asdict(manifest))
    return manifest


def load_manifest(harness_root: Path, run_id: str) -> FixtureManifest:
    path = run_root(harness_root, run_id) / "control" / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        role_output_paths = tuple(
            (str(item[0]), str(item[1]))
            for item in raw["role_output_paths"]
        )
        manifest = FixtureManifest(
            schema_version=int(raw["schema_version"]),
            run_id=str(raw["run_id"]),
            fixture_path=str(raw["fixture_path"]),
            source_digest=str(raw["source_digest"]),
            implementation_target_digest=str(
                raw["implementation_target_digest"]
            ),
            role_output_paths=role_output_paths,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PermissionSpikeError(f"invalid fixture manifest: {path}") from exc

    fixture = expected_fixture_path(harness_root, run_id)
    if manifest.schema_version != 1 or manifest.run_id != run_id:
        raise PermissionSpikeError("fixture manifest provenance mismatch")
    if Path(manifest.fixture_path).resolve() != fixture:
        raise PermissionSpikeError("fixture manifest path mismatch")
    if not (fixture / MARKER_NAME).is_file():
        raise PermissionSpikeError("disposable fixture marker is missing")
    if {role for role, _ in role_output_paths} != set(ALL_ROLES):
        raise PermissionSpikeError("fixture manifest role set mismatch")
    return manifest


def record_worker_result(
    harness_root: Path,
    run_id: str,
    role: str,
    status: ValidationStatus,
    read_value: str | None,
    source_write_blocked: bool,
    out_write_succeeded: bool,
    implementation_write_succeeded: bool,
    runtime_ids: Sequence[str],
    evidence: Sequence[str],
) -> Path:
    manifest = load_manifest(harness_root, run_id)
    role_paths = dict(manifest.role_output_paths)
    if role not in role_paths:
        raise PermissionSpikeError(f"unknown permission role: {role}")
    if not runtime_ids or not all(str(item) for item in runtime_ids):
        raise PermissionSpikeError("at least one runtime ID is required")
    if not evidence or not all(str(item) for item in evidence):
        raise PermissionSpikeError("at least one evidence item is required")
    output = Path(role_paths[role]).resolve()
    expected_parent = (
        run_root(harness_root, run_id)
        / "permission-steps"
        / role
        / "out"
    ).resolve()
    if output.parent != expected_parent:
        raise PermissionSpikeError("worker result path escaped its role outbox")
    value = {
        "role": role,
        "status": status.value,
        "read_value": read_value,
        "source_write_blocked": source_write_blocked,
        "out_write_succeeded": out_write_succeeded,
        "implementation_write_succeeded": implementation_write_succeeded,
        "runtime_ids": [str(item) for item in runtime_ids],
        "evidence": [str(item) for item in evidence],
    }
    write_json_atomic(output, value)
    return output


def parse_worker_result(path: Path, expected_role: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "role": expected_role,
            "status": ValidationStatus.BLOCKED.value,
            "read_value": None,
            "source_write_blocked": False,
            "out_write_succeeded": False,
            "implementation_write_succeeded": False,
            "runtime_ids": [],
            "evidence": [f"missing worker result: {path}"],
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "role": expected_role,
            "status": ValidationStatus.FAIL.value,
            "read_value": None,
            "source_write_blocked": False,
            "out_write_succeeded": False,
            "implementation_write_succeeded": False,
            "runtime_ids": [],
            "evidence": [f"invalid worker result {path}: {exc}"],
        }

    required = {
        "role",
        "status",
        "read_value",
        "source_write_blocked",
        "out_write_succeeded",
        "implementation_write_succeeded",
        "runtime_ids",
        "evidence",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PermissionSpikeError(
            f"worker result schema mismatch for {expected_role}: {path}"
        )
    if value["role"] != expected_role:
        raise PermissionSpikeError(
            f"worker result role mismatch: {value['role']!r}"
        )
    if value["status"] not in {item.value for item in ValidationStatus}:
        raise PermissionSpikeError("worker result status is invalid")
    if not isinstance(value["runtime_ids"], list) or not all(
        isinstance(item, str) and item
        for item in value["runtime_ids"]
    ):
        raise PermissionSpikeError("worker runtime_ids must be nonempty strings")
    if not isinstance(value["evidence"], list) or not all(
        isinstance(item, str) and item
        for item in value["evidence"]
    ):
        raise PermissionSpikeError("worker evidence must be nonempty strings")
    for field in (
        "source_write_blocked",
        "out_write_succeeded",
        "implementation_write_succeeded",
    ):
        if not isinstance(value[field], bool):
            raise PermissionSpikeError(f"worker field {field} must be bool")
    return value


def check(
    check_id: str,
    passed: bool,
    evidence: Sequence[str],
    blocked: bool = False,
) -> PermissionCheck:
    status = (
        ValidationStatus.PASS
        if passed
        else ValidationStatus.BLOCKED
        if blocked
        else ValidationStatus.FAIL
    )
    return PermissionCheck(
        check_id=check_id,
        status=status,
        evidence=tuple(str(item) for item in evidence if str(item)),
    )


def build_report(
    harness_root: Path,
    run_id: str,
    strategy: PermissionStrategy,
    orca_version: str,
) -> PermissionFeasibilityReport:
    manifest = load_manifest(harness_root, run_id)
    fixture = Path(manifest.fixture_path)
    role_paths = dict(manifest.role_output_paths)
    results = {
        role: parse_worker_result(Path(role_paths[role]), role)
        for role in ALL_ROLES
    }

    source_unchanged = sha256_file(fixture / SOURCE_NAME) == manifest.source_digest
    implementation_changed_as_approved = (
        (fixture / IMPLEMENTATION_TARGET_NAME).read_text(
            encoding="utf-8"
        )
        == IMPLEMENTATION_EXPECTED
    )
    planner = results["claude_planner"]
    claude_review = results["claude_code_review"]
    codex_review = results["codex_review"]
    implementer = results["codex_implementer"]

    expected_read = "permission spike source baseline"
    checks = (
        check(
            "V-PERM-01",
            planner["read_value"] == expected_read,
            (
                f"claude_planner.read_value={planner['read_value']!r}",
                *planner["evidence"],
            ),
            planner["status"] == ValidationStatus.BLOCKED.value,
        ),
        check(
            "V-PERM-02",
            source_unchanged
            and bool(planner["source_write_blocked"])
            and bool(claude_review["source_write_blocked"]),
            (
                f"source_unchanged={source_unchanged}",
                f"planner_blocked={planner['source_write_blocked']}",
                f"code_review_blocked={claude_review['source_write_blocked']}",
                *planner["evidence"],
                *claude_review["evidence"],
            ),
            planner["status"] == ValidationStatus.BLOCKED.value
            or claude_review["status"] == ValidationStatus.BLOCKED.value,
        ),
        check(
            "V-PERM-03",
            bool(planner["out_write_succeeded"])
            and bool(claude_review["out_write_succeeded"]),
            (
                f"planner_out={planner['out_write_succeeded']}",
                f"code_review_out={claude_review['out_write_succeeded']}",
                *planner["evidence"],
                *claude_review["evidence"],
            ),
            planner["status"] == ValidationStatus.BLOCKED.value
            or claude_review["status"] == ValidationStatus.BLOCKED.value,
        ),
        check(
            "V-PERM-04",
            source_unchanged
            and bool(codex_review["source_write_blocked"])
            and bool(codex_review["out_write_succeeded"]),
            (
                f"source_unchanged={source_unchanged}",
                f"codex_review_blocked={codex_review['source_write_blocked']}",
                f"codex_review_out={codex_review['out_write_succeeded']}",
                *codex_review["evidence"],
            ),
            codex_review["status"] == ValidationStatus.BLOCKED.value,
        ),
        check(
            "V-PERM-05",
            implementation_changed_as_approved
            and bool(implementer["implementation_write_succeeded"]),
            (
                f"approved_target={implementation_changed_as_approved}",
                f"implementer_reported={implementer['implementation_write_succeeded']}",
                *implementer["evidence"],
            ),
            implementer["status"] == ValidationStatus.BLOCKED.value,
        ),
    )
    if tuple(item.check_id for item in checks) != CHECK_IDS:
        raise PermissionSpikeError("internal permission check order mismatch")

    report_status = (
        ValidationStatus.PASS
        if all(item.status is ValidationStatus.PASS for item in checks)
        else ValidationStatus.BLOCKED
        if any(item.status is ValidationStatus.BLOCKED for item in checks)
        else ValidationStatus.FAIL
    )
    runtime_ids = tuple(
        sorted(
            {
                str(runtime_id)
                for result in results.values()
                for runtime_id in result["runtime_ids"]
            }
        )
    )
    canonical_path = str(
        (run_root(harness_root, run_id) / "control" / REPORT_NAME).resolve()
    )
    evidence = [
        f"fixture={fixture}",
        f"source_digest={sha256_file(fixture / SOURCE_NAME)}",
        f"implementation_target_digest="
        f"{sha256_file(fixture / IMPLEMENTATION_TARGET_NAME)}",
        *(f"runtime_id={runtime_id}" for runtime_id in runtime_ids),
    ]
    without_digest = {
        "schema_version": 1,
        "run_id": run_id,
        "status": report_status.value,
        "strategy": strategy.value if report_status is ValidationStatus.PASS else None,
        "checks": [
            {
                "check_id": item.check_id,
                "status": item.status.value,
                "evidence": list(item.evidence),
            }
            for item in checks
        ],
        "evidence": evidence,
        "orca_version": orca_version,
        "canonical_path": canonical_path,
    }
    digest = sha256_bytes(canonical_json_bytes(without_digest))
    return PermissionFeasibilityReport(
        schema_version=1,
        run_id=run_id,
        status=report_status,
        strategy=(
            strategy if report_status is ValidationStatus.PASS else None
        ),
        checks=checks,
        evidence=tuple(without_digest["evidence"]),
        orca_version=orca_version,
        canonical_path=canonical_path,
        report_digest=digest,
    )


def serialize_report(report: PermissionFeasibilityReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "run_id": report.run_id,
        "status": report.status.value,
        "strategy": report.strategy.value if report.strategy else None,
        "checks": [
            {
                "check_id": item.check_id,
                "status": item.status.value,
                "evidence": list(item.evidence),
            }
            for item in report.checks
        ],
        "evidence": list(report.evidence),
        "orca_version": report.orca_version,
        "canonical_path": report.canonical_path,
        "report_digest": report.report_digest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Orca permission feasibility first gate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "record", "finalize"):
        child = subparsers.add_parser(command)
        child.add_argument("--harness-root", type=Path, required=True)
        child.add_argument("--run-id", required=True)
        if command == "record":
            child.add_argument("--role", choices=ALL_ROLES, required=True)
            child.add_argument(
                "--status",
                choices=[item.value for item in ValidationStatus],
                required=True,
            )
            child.add_argument("--read-value")
            child.add_argument(
                "--source-write-blocked",
                action="store_true",
            )
            child.add_argument(
                "--out-write-succeeded",
                action="store_true",
            )
            child.add_argument(
                "--implementation-write-succeeded",
                action="store_true",
            )
            child.add_argument(
                "--runtime-id",
                action="append",
                required=True,
            )
            child.add_argument(
                "--evidence",
                action="append",
                required=True,
            )
        if command == "finalize":
            child.add_argument(
                "--strategy",
                choices=[item.value for item in PermissionStrategy],
                required=True,
            )
            child.add_argument("--orca-version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = create_fixture(args.harness_root, args.run_id)
            print(
                json.dumps(
                    asdict(manifest),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "record":
            output = record_worker_result(
                args.harness_root,
                args.run_id,
                args.role,
                ValidationStatus(args.status),
                args.read_value,
                args.source_write_blocked,
                args.out_write_succeeded,
                args.implementation_write_succeeded,
                args.runtime_id,
                args.evidence,
            )
            print(output)
            return 0

        report = build_report(
            args.harness_root,
            args.run_id,
            PermissionStrategy(args.strategy),
            args.orca_version,
        )
        report_value = serialize_report(report)
        report_path = Path(report.canonical_path)
        write_json_atomic(report_path, report_value)
        print(json.dumps(report_value, ensure_ascii=False, indent=2))
        return 0 if report.status is ValidationStatus.PASS else 3
    except PermissionSpikeError as exc:
        print(f"permission spike error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
