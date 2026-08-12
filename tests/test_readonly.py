from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.readonly import ReadOnlyMirrorError, prepare_readonly_mirror


class ReadOnlyMirrorTest(unittest.TestCase):
    def test_mirror_excludes_git_runs_and_copies_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "target"
            source.mkdir()
            subprocess.run(
                ("git", "init"),
                cwd=source,
                shell=False,
                capture_output=True,
                check=True,
            )
            (source / "source.txt").write_text("source", encoding="utf-8")
            (source / "runs" / "target-runtime").mkdir(parents=True)
            review_root = root / "harness" / "runs" / "run-1" / "review"
            mirror = prepare_readonly_mirror(
                source,
                review_root,
                1,
                apply_permissions=False,
            )
            self.assertEqual(
                "source",
                (mirror / "source.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse((mirror / ".git").exists())
            self.assertFalse((mirror / "runs").exists())

    def test_mirror_rejects_destination_inside_target_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve()
            subprocess.run(
                ("git", "init"),
                cwd=source,
                shell=False,
                capture_output=True,
                check=True,
            )

            with self.assertRaisesRegex(
                ReadOnlyMirrorError,
                "review_root must be outside the target worktree",
            ):
                prepare_readonly_mirror(
                    source,
                    source / "review",
                    1,
                    apply_permissions=False,
                )

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression")
    def test_windows_mirror_is_readable_but_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "target"
            source.mkdir()
            subprocess.run(
                ("git", "init"),
                cwd=source,
                shell=False,
                capture_output=True,
                check=True,
            )
            (source / "source.txt").write_text("source", encoding="utf-8")
            mirror = prepare_readonly_mirror(
                source,
                root / "harness" / "review",
                1,
            )
            identity = subprocess.run(
                ("whoami",),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
            try:
                mirrored_source = mirror / "source.txt"
                self.assertEqual(
                    "source",
                    mirrored_source.read_text(encoding="utf-8"),
                )
                with self.assertRaises(PermissionError):
                    mirrored_source.write_text("changed", encoding="utf-8")
            finally:
                subprocess.run(
                    (
                        "icacls",
                        str(mirror),
                        "/grant:r",
                        f"{identity}:(F)",
                        "/T",
                        "/C",
                    ),
                    shell=False,
                    capture_output=True,
                    check=True,
                )
