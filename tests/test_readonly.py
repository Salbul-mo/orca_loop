from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.readonly import ReadOnlyMirrorError, prepare_readonly_mirror


class ReadOnlyMirrorTest(unittest.TestCase):
    def test_mirror_excludes_git_runs_and_copies_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(
                ("git", "init"),
                cwd=root,
                shell=False,
                capture_output=True,
                check=True,
            )
            (root / "source.txt").write_text("source", encoding="utf-8")
            (root / "runs" / "run-1" / "review").mkdir(parents=True)
            mirror = prepare_readonly_mirror(
                root,
                root / "runs" / "run-1" / "review",
                1,
                apply_permissions=False,
            )
            self.assertEqual(
                "source",
                (mirror / "source.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse((mirror / ".git").exists())
            self.assertFalse((mirror / "runs").exists())

    def test_external_repository_requires_exact_trusted_review_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "target"
            source.mkdir()
            subprocess.run(("git", "init"), cwd=source, check=True, capture_output=True)
            (source / "file.py").write_text("safe = 1", encoding="utf-8")
            runs = base / "harness" / "runs"
            review = runs / "run-1" / "review"
            review.mkdir(parents=True)
            with self.assertRaises(ReadOnlyMirrorError):
                prepare_readonly_mirror(source, review, 0, apply_permissions=False)
            with self.assertRaises(ReadOnlyMirrorError):
                prepare_readonly_mirror(
                    source, review, 0, apply_permissions=False,
                    trusted_runs_root=base / "untrusted",
                )
            mirror = prepare_readonly_mirror(
                source, review, 0, apply_permissions=False,
                trusted_runs_root=runs,
            )
            self.assertEqual("safe = 1", (mirror / "file.py").read_text(encoding="utf-8"))
            self.assertEqual("safe = 1", (source / "file.py").read_text(encoding="utf-8"))
            self.assertFalse((mirror / ".git").exists())
