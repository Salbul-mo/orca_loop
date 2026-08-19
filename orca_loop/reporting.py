"""Human-readable, durable output for every stage of a run.

The coordinator's own state is a chain of machine-readable generations, and
the promoted artifacts are strict contract JSON that gets overwritten on each
revision. Neither survives a failed run in a form anyone can read, so this
module keeps an immutable copy of every promoted artifact and renders the run
as Markdown next to it.

Every entry point here is best-effort: reporting runs after the durable
commit, and a reporting failure must never turn a working run into a failed
one. Errors are appended to ``logs/reporting.log`` and swallowed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPORTS_DIRNAME = "reports"
HISTORY_DIRNAME = "history"
SUMMARY_NAME = "00-run-summary.md"
FAILURE_NAME = "99-failure.md"
REPORTING_LOG = "reporting.log"

STAGE_REPORTS: tuple[tuple[str, str, str], ...] = (
    ("plan", "01-plan.md", "계획 (Planner)"),
    ("plan_review", "02-plan-review.md", "계획 검토 (Plan Reviewer)"),
    ("implementation", "03-implementation.md", "구현 (Implementer)"),
    ("code_review_a", "04-code-review-a.md", "Blind 코드 검토 A"),
    ("code_review_b", "05-code-review-b.md", "Blind 코드 검토 B"),
    ("review_comparison", "06-review-comparison.md", "검토 비교"),
    (
        "review_adjudication_a",
        "07-review-adjudication-a.md",
        "검토 재정 A",
    ),
    (
        "review_adjudication_b",
        "08-review-adjudication-b.md",
        "검토 재정 B",
    ),
    ("code_review", "09-legacy-code-review.md", "Legacy 코드 검토"),
    ("cross_review", "10-legacy-cross-review.md", "Legacy 교차 확인"),
)
STAGE_TITLE = {kind: title for kind, _, title in STAGE_REPORTS}
STAGE_FILENAME = {kind: name for kind, name, _ in STAGE_REPORTS}


def _log_failure(run_root: Path, context: str, error: BaseException) -> None:
    try:
        log_dir = run_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (log_dir / REPORTING_LOG).open(
            "a",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(f"{stamp} {context}: {error!r}\n")
    except OSError:
        return


def best_effort(context: str) -> Callable:
    """Run a reporting step without letting it fail the surrounding run."""

    def decorate(function: Callable) -> Callable:
        def wrapper(run_root: Path, *args: Any, **kwargs: Any):
            try:
                return function(run_root, *args, **kwargs)
            except (OSError, ValueError, TypeError, KeyError) as error:
                _log_failure(run_root, context, error)
                return None

        wrapper.__name__ = function.__name__
        wrapper.__doc__ = function.__doc__
        return wrapper

    return decorate


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [value]


def _bullets(values: Sequence[object], empty: str = "없음") -> str:
    items = [str(item) for item in values if str(item).strip()]
    if not items:
        return f"- {empty}\n"
    return "".join(f"- {item}\n" for item in items)


@best_effort("record_artifact_history")
def record_artifact_history(
    run_root: Path,
    artifact_kind: str,
    generation: int,
    raw: bytes,
) -> Path | None:
    """Keep an immutable per-generation copy of a promoted artifact."""
    target = (
        run_root
        / "artifacts"
        / HISTORY_DIRNAME
        / f"{artifact_kind}.g{generation:04d}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)
    return target


def _render_plan(value: Mapping[str, object]) -> str:
    files = _as_list(value.get("affected_files"))
    criteria = _as_list(value.get("acceptance_criteria"))
    contract = value.get("test_contract")
    commands = (
        _as_list(contract.get("commands"))
        if isinstance(contract, dict)
        else []
    )
    lines = [
        f"- plan_version: {value.get('plan_version')}\n",
        "\n## 요구 해석\n\n",
        f"{value.get('interpretation', '')}\n",
        "\n## 근거\n\n",
        f"{value.get('rationale', '')}\n",
        "\n## 현재 상태 증거\n\n",
        _bullets(_as_list(value.get("current_state_evidence"))),
        "\n## 변경 대상 파일\n\n",
        _bullets(
            [
                f"`{item.get('path')}` ({item.get('operation')})"
                if isinstance(item, dict)
                else item
                for item in files
            ]
        ),
        "\n## 구현 단계\n\n",
        _bullets(_as_list(value.get("implementation_steps"))),
        "\n## 수용 기준\n\n",
        _bullets(
            [
                f"{item.get('criterion_id')}: "
                f"{item.get('verification_method', item.get('statement', ''))}"
                if isinstance(item, dict)
                else item
                for item in criteria
            ]
        ),
        "\n## 테스트 계약\n\n",
        _bullets(
            [
                (
                    " ".join(str(part) for part in _as_list(item.get("argv")))
                    + f" (cwd={item.get('cwd')}, kind={item.get('kind')}, "
                    + f"timeout_ms={item.get('timeout_ms')})"
                )
                if isinstance(item, dict)
                else (
                    " ".join(str(part) for part in item)
                    if isinstance(item, (list, tuple))
                    else str(item)
                )
                for item in commands
            ]
        ),
        "\n## 데이터/API/스키마 변경\n\n",
        f"{value.get('data_api_schema_changes', '없음')}\n",
        "\n## 위험\n\n",
        _bullets(_as_list(value.get("risks"))),
        "\n## 범위 밖\n\n",
        _bullets(_as_list(value.get("out_of_scope"))),
    ]
    return "".join(lines)


def _render_finding(item: object) -> str:
    if not isinstance(item, dict):
        return f"- {item}\n"
    header = (
        f"### {item.get('finding_id')} "
        f"[{item.get('severity')} / {item.get('blocking_reason')} / "
        f"{item.get('impact_class')}]\n\n"
    )
    location = item.get("file")
    if location:
        line = item.get("line")
        header += (
            f"- 위치: `{location}`"
            + (f":{line}" if line else "")
            + "\n"
        )
    body = [
        header,
        f"- 원인: {item.get('root_cause', '')}\n",
        f"- 설명: {item.get('description', '')}\n",
    ]
    if item.get("required_fix"):
        body.append(f"- 요구 수정: {item['required_fix']}\n")
    if item.get("required_change"):
        body.append(f"- 요구 변경: {item['required_change']}\n")
    if item.get("reopens"):
        body.append(f"- 재개: {item['reopens']}\n")
    body.append("\n")
    return "".join(body)


def _render_review(value: Mapping[str, object]) -> str:
    findings = _as_list(value.get("findings"))
    suggestions = _as_list(value.get("non_blocking_suggestions"))
    lines = [
        f"- verdict: **{value.get('verdict')}**\n",
        f"- consensus_round: {value.get('consensus_round')}\n",
        f"- reviewed_plan_version: {value.get('reviewed_plan_version')}\n",
        f"- 차단 finding 수: {len(findings)}\n",
        "\n## Findings\n\n",
    ]
    if not findings:
        lines.append("차단 finding 없음\n")
    else:
        lines.extend(_render_finding(item) for item in findings)
    lines.append("\n## 비차단 제안\n\n")
    lines.append(
        _bullets(
            [
                item.get("description", str(item))
                if isinstance(item, dict)
                else item
                for item in suggestions
            ]
        )
    )
    return "".join(lines)


def _render_implementation(value: Mapping[str, object]) -> str:
    return "".join(
        [
            f"- status: **{value.get('status')}**\n",
            f"- consensus_round: {value.get('consensus_round')}\n",
            f"- plan_change_required: {value.get('plan_change_required')}\n",
            "\n## 요약\n\n",
            f"{value.get('summary', '')}\n",
            "\n## 변경 파일\n\n",
            _bullets(
                [f"`{item}`" for item in _as_list(value.get("changed_files"))]
            ),
            "\n## 처리한 finding\n\n",
            _bullets(
                [
                    f"{item.get('finding_id')}: "
                    + ", ".join(
                        str(ref)
                        for ref in _as_list(item.get("evidence_refs"))
                    )
                    if isinstance(item, dict)
                    else item
                    for item in _as_list(value.get("addressed_findings"))
                ]
            ),
        ]
    )


def _render_body(artifact_kind: str, value: Mapping[str, object]) -> str:
    if artifact_kind == "plan":
        return _render_plan(value)
    if artifact_kind == "implementation":
        return _render_implementation(value)
    if artifact_kind in {
        "plan_review",
        "code_review_a",
        "code_review_b",
        "code_review",
        "cross_review",
    }:
        return _render_review(value)
    return "```json\n" + json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n```\n"


@best_effort("render_stage_report")
def render_stage_report(
    run_root: Path,
    artifact_kind: str,
    raw_text: str,
    generation: int,
) -> Path | None:
    """Render one promoted artifact as Markdown under ``reports/``."""
    filename = STAGE_FILENAME.get(artifact_kind)
    if filename is None:
        return None
    value = json.loads(raw_text)
    if not isinstance(value, dict):
        return None
    title = STAGE_TITLE[artifact_kind]
    header = (
        f"# {title}\n\n"
        f"생성: {_utc_now()}  |  generation: g{generation:04d}  |  "
        f"artifact: `artifacts/{artifact_kind}.json`\n\n"
    )
    return _write_text(
        run_root / REPORTS_DIRNAME / filename,
        header + _render_body(artifact_kind, value),
    )


def resume_command_line(harness_root: Path, run_id: str) -> str:
    return (
        f'py -3 "{harness_root / "run_loop.py"}" resume --run-id {run_id}'
    )


def _stage_rows(run_root: Path) -> str:
    rows = [
        "| 단계 | 상태 | artifact | 리포트 |",
        "| --- | --- | --- | --- |",
    ]
    for kind, filename, title in STAGE_REPORTS:
        artifact = run_root / "artifacts" / f"{kind}.json"
        report = run_root / REPORTS_DIRNAME / filename
        history = sorted(
            (run_root / "artifacts" / HISTORY_DIRNAME).glob(f"{kind}.g*.json")
        )
        status = "완료" if artifact.is_file() else "미완료"
        revision = f" ({len(history)}회)" if len(history) > 1 else ""
        rows.append(
            f"| {title} | {status}{revision} | "
            + (f"`artifacts/{kind}.json`" if artifact.is_file() else "—")
            + " | "
            + (f"`reports/{filename}`" if report.is_file() else "—")
            + " |"
        )
    return "\n".join(rows) + "\n"


@best_effort("render_run_summary")
def render_run_summary(
    run_root: Path,
    state,
    ledger,
    *,
    harness_root: Path,
    last_error: str | None = None,
) -> Path | None:
    """Rewrite the run summary; called after every committed generation."""
    unresolved = sum(
        1
        for record in ledger.findings
        if record.status.value != "RESOLVED"
    )
    history = state.history[-10:]
    provider_policy = "UNKNOWN"
    independence = "UNKNOWN"
    manifest_path = run_root / "control" / "run-manifest.json"
    if manifest_path.is_file():
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest_value, dict):
            provider_policy = str(
                manifest_value.get("consensus_provider_policy", "UNKNOWN")
            )
            independence = str(
                manifest_value.get("consensus_independence", "UNKNOWN")
            )
    lines = [
        f"# Orca Loop Run Summary — {state.run_id}\n\n",
        f"갱신: {_utc_now()}  |  상태: **{state.status.value}** / "
        f"**{state.state.value}**  |  generation: {state.generation}\n\n",
        "## 단계별 진행\n\n",
        _stage_rows(run_root),
        "\n## Consensus\n\n",
        f"- plan_round: {ledger.plan_round}\n",
        f"- code_round: {ledger.code_round}\n",
        f"- 미해결 finding: {unresolved}\n",
        f"- test gate: {state.test_gate_status.value if state.test_gate_status else 'NOT RUN'}\n",
        f"- provider policy: {provider_policy}\n",
        f"- consensus independence: {independence}\n",
        "\n## 최근 전이\n\n",
    ]
    if not history:
        lines.append("- 없음\n")
    else:
        lines.extend(
            f"- g{item.generation:04d} {item.state.value} / "
            f"{item.step_stage.value} ({item.signal.value}): {item.reason}\n"
            for item in history
        )
    if last_error:
        lines.extend(["\n## 마지막 오류\n\n", f"```\n{last_error}\n```\n"])
    lines.extend(
        [
            "\n## 증거 위치\n\n",
            "- 단계별 산출물: `reports/`\n",
            "- artifact 이력: `artifacts/history/`\n",
            "- worker 실행 로그: `logs/step-*.stdout.log`, "
            "`logs/step-*.stderr.log`, `logs/step-*.runner.json`\n",
            "- 재시작 이벤트: `control/resume-events.jsonl`\n",
            "\n## 재시작 방법\n\n",
            "```text\n",
            resume_command_line(harness_root, state.run_id) + "\n",
            "```\n",
        ]
    )
    return _write_text(run_root / REPORTS_DIRNAME / SUMMARY_NAME, "".join(lines))


@best_effort("render_failure_report")
def render_failure_report(
    run_root: Path,
    *,
    reason: str,
    harness_root: Path,
    run_id: str,
    detail: Sequence[str] = (),
) -> Path | None:
    """Record why a run stopped and exactly how to restart it."""
    logs = run_root / "logs"
    evidence = (
        sorted(str(item.relative_to(run_root)) for item in logs.iterdir())
        if logs.is_dir()
        else []
    )
    lines = [
        f"# 중단 보고 — {run_id}\n\n",
        f"기록: {_utc_now()}\n\n",
        "## 원인\n\n",
        f"```\n{reason}\n```\n",
    ]
    if detail:
        lines.extend(["\n## 상세\n\n", _bullets(list(detail))])
    lines.extend(
        [
            "\n## 보존된 증거\n\n",
            _bullets(evidence[:40], empty="로그 파일 없음"),
            "\n## 재시작 방법\n\n",
            "```text\n",
            resume_command_line(harness_root, run_id) + "\n",
            "```\n",
            "\nIMPLEMENT/FIX 단계에서 worktree가 변경된 상태라면 변경 내용을 "
            "먼저 확인한 뒤 `--accept-worktree-drift`를 덧붙여 재시작한다.\n",
        ]
    )
    return _write_text(run_root / REPORTS_DIRNAME / FAILURE_NAME, "".join(lines))
