from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_loop.bootstrap import (
    OrcaRepositoryRegistrationError,
    bootstrap_repository,
    register_orca_repository,
)
from orca_loop.workspace import (
    PathBoundaryError,
    RunWorkspaceExistsError,
    create_run_workspace,
)


class BootstrapTest(unittest.TestCase):
    def test_bootstrap_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = bootstrap_repository(root)
            second = bootstrap_repository(root)
            self.assertTrue(first.repo_initialized)
            self.assertTrue(second.package_importable)
            self.assertTrue((root / ".git").is_dir())

    def test_registration_requires_one_git_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bootstrap_repository(root)
            responses = [
                _completed({"ok": True, "result": {"repo": {}}}),
                _completed(
                    {
                        "ok": True,
                        "result": {
                            "repos": [
                                {
                                    "id": "repo-1",
                                    "path": str(root),
                                    "kind": "git",
                                }
                            ]
                        },
                    }
                ),
            ]
            with patch(
                "orca_loop.bootstrap._run",
                side_effect=responses,
            ) as runner:
                report = register_orca_repository(root, "orca")
            self.assertEqual("repo-1", report.repo_id)
            self.assertEqual("git", report.kind)
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertFalse(
                any("remove" in command or "delete" in command for command in commands)
            )

    def test_duplicate_or_folder_registration_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bootstrap_repository(root)
            for repos in (
                [
                    {"id": "one", "path": str(root), "kind": "git"},
                    {"id": "two", "path": str(root), "kind": "git"},
                ],
                [{"id": "one", "path": str(root), "kind": "folder"}],
            ):
                responses = [
                    _completed({"ok": True, "result": {"repo": {}}}),
                    _completed({"ok": True, "result": {"repos": repos}}),
                ]
                with patch(
                    "orca_loop.bootstrap._run",
                    side_effect=responses,
                ):
                    with self.assertRaises(
                        OrcaRepositoryRegistrationError
                    ):
                        register_orca_repository(root, "orca")


class WorkspaceTest(unittest.TestCase):
    def test_rejects_traversal_and_non_resume_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaises(PathBoundaryError):
                create_run_workspace(
                    root,
                    "../escape",
                    "step-1",
                    resume=False,
                )
            create_run_workspace(root, "run-1", "step-1", resume=False)
            with self.assertRaises(RunWorkspaceExistsError):
                create_run_workspace(
                    root,
                    "run-1",
                    "step-2",
                    resume=False,
                )

    def test_resume_reuses_run_and_keeps_control_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run, first = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            resumed, second = create_run_workspace(
                root,
                "run-1",
                "step-2",
                resume=True,
            )
            self.assertEqual(run.root, resumed.root)
            self.assertNotEqual(first.root, second.root)
            self.assertNotEqual(run.control_dir, second.input_dir)
            self.assertNotEqual(run.control_dir, second.output_dir)


def _completed(value: dict[str, object]):
    from subprocess import CompletedProcess

    return CompletedProcess(
        args=("orca",),
        returncode=0,
        stdout=json.dumps(value),
        stderr="",
    )


if __name__ == "__main__":
    unittest.main()
