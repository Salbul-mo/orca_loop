from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orca_loop.models import AffectedFile, AffectedFileOperation
from orca_loop.snapshot import (
    canonical_content,
    capture_snapshot,
    materialize_frozen_review,
)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self._git("init")
        self._git("config", "user.name", "snapshot-test")
        self._git("config", "user.email", "snapshot-test@invalid.local")
        (self.root / ".gitignore").write_text(
            "review-output/\n",
            encoding="utf-8",
        )
        (self.root / "tracked.txt").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        self._git("add", "--", ".")
        self._git("commit", "-m", "baseline")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ("git", "-C", str(self.root), *args),
            check=True,
            capture_output=True,
        )

    def test_untracked_order_does_not_depend_on_creation_order(self) -> None:
        (self.root / "z.txt").write_text("z\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        first = capture_snapshot(self.root)
        self.assertEqual(("a.txt", "z.txt"), tuple(x[0] for x in first.untracked))
        second = capture_snapshot(self.root)
        self.assertEqual(first, second)

    def test_text_line_endings_are_canonical_and_binary_changes_detected(self) -> None:
        self.assertEqual(
            canonical_content(b"line\r\n"),
            canonical_content(b"line\n"),
        )
        binary = self.root / "binary.bin"
        binary.write_bytes(b"\x00\x01")
        first = capture_snapshot(self.root)
        binary.write_bytes(b"\x00\x02")
        second = capture_snapshot(self.root)
        self.assertNotEqual(first.snapshot_digest, second.snapshot_digest)

    def test_frozen_review_matches_snapshot_and_manifest(self) -> None:
        (self.root / "tracked.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        snapshot = capture_snapshot(self.root)
        frozen = materialize_frozen_review(
            self.root,
            snapshot,
            (
                AffectedFile(
                    path="tracked.txt",
                    operation=AffectedFileOperation.MODIFY,
                    rename_from=None,
                ),
            ),
            self.root / "review-output",
            destructive_approval_digest=None,
        )
        self.assertEqual(snapshot.snapshot_digest, frozen.snapshot_digest)
        self.assertTrue(frozen.diff_path.is_file())
        self.assertIn(
            snapshot.snapshot_digest,
            frozen.manifest_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(snapshot, capture_snapshot(self.root))


if __name__ == "__main__":
    unittest.main()
