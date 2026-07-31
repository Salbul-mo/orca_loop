from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.readonly import prepare_readonly_mirror


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
