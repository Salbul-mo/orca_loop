from __future__ import annotations

import re
from pathlib import Path

from .models import RunWorkspace, StepWorkspace


ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class WorkspaceError(RuntimeError):
    """Base error for run and step workspace management."""


class RunWorkspaceExistsError(WorkspaceError):
    """Raised when new/resume workspace semantics are violated."""


class PathBoundaryError(WorkspaceError):
    """Raised when a run or step path escapes the harness boundary."""


def _validate_id(value: str, field: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        raise PathBoundaryError(
            f"{field} must match [A-Za-z0-9_-]{{1,80}}"
        )


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def agent_runtime_snapshot_path(harness_root: Path, run_id: str) -> Path:
    _validate_id(run_id, "run_id")
    root = harness_root.resolve()
    if not root.is_dir():
        raise PathBoundaryError(f"harness_root does not exist: {root}")
    runs_root = (root / "runs").resolve()
    run_root = (runs_root / run_id).resolve()
    if not _within(run_root, runs_root):
        raise PathBoundaryError("run path escaped harness_root/runs")
    return run_root / "control" / "agent-runtime.json"


def master_runtime_snapshot_path(harness_root: Path, run_id: str) -> Path:
    _validate_id(run_id, "run_id")
    root = harness_root.resolve()
    if not root.is_dir():
        raise PathBoundaryError(f"harness_root does not exist: {root}")
    runs_root = (root / "runs").resolve()
    run_root = (runs_root / run_id).resolve()
    if not _within(run_root, runs_root):
        raise PathBoundaryError("run path escaped harness_root/runs")
    return run_root / "control" / "master-runtime.json"


def create_run_workspace(
    harness_root: Path,
    run_id: str,
    step_id: str,
    *,
    resume: bool,
) -> tuple[RunWorkspace, StepWorkspace]:
    _validate_id(run_id, "run_id")
    _validate_id(step_id, "step_id")
    root = harness_root.resolve()
    if not root.is_dir():
        raise PathBoundaryError(f"harness_root does not exist: {root}")
    runs_root = (root / "runs").resolve()
    run_root = (runs_root / run_id).resolve()
    if not _within(run_root, runs_root):
        raise PathBoundaryError("run path escaped harness_root/runs")

    if run_root.exists() and any(run_root.iterdir()):
        if not resume:
            raise RunWorkspaceExistsError(
                f"non-resume run already exists: {run_root}"
            )
    elif resume:
        raise RunWorkspaceExistsError(
            f"resume requested for missing run: {run_root}"
        )

    control_dir = run_root / "control"
    artifact_dir = run_root / "artifacts"
    review_dir = run_root / "review"
    steps_dir = run_root / "steps"
    log_dir = run_root / "logs"
    for directory in (
        control_dir,
        artifact_dir,
        review_dir,
        steps_dir,
        log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    step_root = (steps_dir / step_id).resolve()
    input_dir = step_root / "in"
    output_dir = step_root / "out"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    for candidate in (step_root, input_dir, output_dir):
        if not _within(candidate, run_root):
            raise PathBoundaryError(f"step path escaped run root: {candidate}")
    if (
        input_dir == output_dir
        or _within(input_dir, control_dir)
        or _within(output_dir, control_dir)
    ):
        raise PathBoundaryError("step input/output overlaps control directory")

    run_workspace = RunWorkspace(
        run_id=run_id,
        root=run_root,
        control_dir=control_dir,
        artifact_dir=artifact_dir,
        review_dir=review_dir,
        steps_dir=steps_dir,
        log_dir=log_dir,
    )
    step_workspace = StepWorkspace(
        step_id=step_id,
        root=step_root,
        input_dir=input_dir,
        output_dir=output_dir,
    )
    return run_workspace, step_workspace
