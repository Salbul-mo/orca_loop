"""Run the repository-local Graft CLI without changing agent configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "runs" / "codex-mhj_26_09_22_04_graft-tool"
CACHE = ROOT / "runs" / "codex-mhj_26_09_22_05_graft-cache"
PACKAGE = TOOL / "node_modules" / "@nanonets" / "graft"
VERSION = "0.12.1"
COMMANDS = ("install", "build", "check", "map", "ask", "grep", "callers", "skeleton")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    environment = os.environ.copy()
    environment["DO_NOT_TRACK"] = "1"
    if args.command == "install":
        if args.arguments:
            parser.error("install does not accept extra arguments")
        executable = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if executable is None:
            parser.error("npm is required; install Node.js >=20 first")
        command = [
            executable, "install", "--prefix", str(TOOL),
            "--no-audit", "--no-fund", "--save-exact", "--ignore-scripts",
            f"@nanonets/graft@{VERSION}",
        ]
    else:
        executable = shutil.which("node")
        if executable is None:
            parser.error("Node.js >=20 is required")
        try:
            installed = json.loads((PACKAGE / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"run this script with install first: {exc}")
        if installed.get("version") != VERSION:
            parser.error(f"expected Graft {VERSION}; run install to restore it")
        if any(
            value == "--deep" or value.startswith("--deep=")
            or value == "--dir" or value.startswith("--dir=")
            for value in args.arguments
        ):
            parser.error("this helper uses structural analysis and a fixed cache directory")
        command = [
            executable, str(PACKAGE / "dist" / "cli.js"),
            "--dir", str(CACHE), args.command, *args.arguments,
        ]
        # Graft resolves the repository from cwd and the explicit cache path.
        # The caller's current directory must not select a different repository.
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=environment, check=False,
            capture_output=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        sys.stdout.buffer.write(result.stdout.encode("utf-8"))
        sys.stderr.buffer.write(result.stderr.encode("utf-8"))
        return result.returncode
    except OSError as exc:
        print(f"Graft launch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
