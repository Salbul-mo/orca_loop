from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orca_loop.config import empty_test_policy
from orca_loop.guards import (
    guard_repository_delta,
    guard_step_sandbox,
)
from orca_loop.models import (
    AffectedFile,
    AffectedFileOperation,
    DestructiveApproval,
    Role,
    SnapshotIdentity,
    StagedInput,
)
from orca_loop.transport import stage_inputs
from orca_loop.workspace import create_run_workspace


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def snapshot(digest: str) -> SnapshotIdentity:
    return SnapshotIdentity("a" * 40, DIGEST_A, DIGEST_A, (), digest)


def approval(
    operations: tuple[AffectedFile, ...],
) -> DestructiveApproval:
    return DestructiveApproval(
        "run-1",
        1,
        DIGEST_A,
        DIGEST_A,
        operations,
        "gate-1",
        DIGEST_B,
    )


class RepositoryGuardTest(unittest.TestCase):
    def test_readonly_and_prefix_bypass_are_rejected(self) -> None:
        report = guard_repository_delta(
            snapshot(DIGEST_A),
            snapshot(DIGEST_B),
            Role.PLAN_REVIEWER,
            (),
            None,
            before_files={"src/a.py": DIGEST_A},
            after_files={"src/a.py": DIGEST_B},
        )
        self.assertFalse(report.ok)
        implement = guard_repository_delta(
            snapshot(DIGEST_A),
            snapshot(DIGEST_B),
            Role.IMPLEMENTER,
            (
                AffectedFile(
                    "src/a.py",
                    AffectedFileOperation.MODIFY,
                    None,
                ),
            ),
            None,
            before_files={"src/abc.py": DIGEST_A},
            after_files={"src/abc.py": DIGEST_B},
        )
        self.assertFalse(implement.ok)

    def test_delete_and_rename_require_exact_approval(self) -> None:
        delete = AffectedFile(
            "old.py",
            AffectedFileOperation.DELETE,
            None,
        )
        blocked = guard_repository_delta(
            snapshot(DIGEST_A),
            snapshot(DIGEST_B),
            Role.IMPLEMENTER,
            (delete,),
            None,
            before_files={"old.py": DIGEST_A},
            after_files={},
        )
        self.assertFalse(blocked.ok)
        allowed = guard_repository_delta(
            snapshot(DIGEST_A),
            snapshot(DIGEST_B),
            Role.IMPLEMENTER,
            (delete,),
            approval((delete,)),
            before_files={"old.py": DIGEST_A},
            after_files={},
        )
        self.assertTrue(allowed.ok)
        rename = AffectedFile(
            "new.py",
            AffectedFileOperation.RENAME,
            "old.py",
        )
        renamed = guard_repository_delta(
            snapshot(DIGEST_A),
            snapshot(DIGEST_B),
            Role.IMPLEMENTER,
            (rename,),
            approval((rename,)),
            before_files={"old.py": DIGEST_A},
            after_files={"new.py": DIGEST_A},
        )
        self.assertTrue(renamed.ok)


class StepGuardTest(unittest.TestCase):
    def test_input_outbox_and_test_output_violations_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, step = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            manifest = stage_inputs(
                step,
                (StagedInput("contract.md", None, b"contract"),),
            )
            (step.input_dir / "contract.md").write_bytes(b"changed")
            foreign = root / "foreign.json"
            foreign.write_text("{}", encoding="utf-8")
            report = guard_step_sandbox(
                step,
                manifest,
                (foreign,),
                test_policy=empty_test_policy(),
                changed_test_paths=("build/output.txt",),
            )
            self.assertFalse(report.ok)
            self.assertEqual(
                {
                    "input_tampered",
                    "outbox_escape",
                    "test_output_scope",
                },
                {item.code for item in report.violations},
            )


if __name__ == "__main__":
    unittest.main()
