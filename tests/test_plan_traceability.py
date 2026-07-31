from __future__ import annotations

import re
import unittest
from pathlib import Path


BLOCK_PATTERN = re.compile(r"^### (M-B\d{2}-\d{2})\b", re.MULTILINE)
ID_PATTERN = re.compile(r"M-B\d{2}-\d{2}")
PRECONDITION_PATTERN = re.compile(
    r"\| \*\*Preconditions\*\* \|(?P<value>.*?)\|",
)
LAYER_PATTERN = re.compile(r"^Layer (?P<layer>\d+)\s+(?P<ids>.*)$")


class PhaseThreeTraceabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.plan_path = (
            cls.root
            / "docs"
            / "codex-mhj_26_07_31_03_phase3-micro-blocking.md"
        )
        cls.text = cls.plan_path.read_text(encoding="utf-8")

    def _blocks(self) -> dict[str, str]:
        matches = list(BLOCK_PATTERN.finditer(self.text))
        values: dict[str, str] = {}
        for index, match in enumerate(matches):
            end = (
                len(self.text)
                if index + 1 == len(matches)
                else matches[index + 1].start()
            )
            block_id = match.group(1)
            self.assertNotIn(block_id, values)
            values[block_id] = self.text[match.start():end]
        return values

    def test_all_39_blocks_have_defined_acyclic_preconditions(self) -> None:
        blocks = self._blocks()
        self.assertEqual(39, len(blocks))
        edges: dict[str, set[str]] = {}
        for block_id, section in blocks.items():
            match = PRECONDITION_PATTERN.search(section)
            self.assertIsNotNone(
                match,
                f"{block_id} has no Preconditions field",
            )
            dependencies = set(ID_PATTERN.findall(match.group("value")))
            undefined = dependencies - set(blocks)
            self.assertFalse(
                undefined,
                f"{block_id} has undefined dependencies {undefined}",
            )
            edges[block_id] = dependencies

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(block_id: str) -> None:
            self.assertNotIn(
                block_id,
                visiting,
                f"dependency cycle reaches {block_id}",
            )
            if block_id in visited:
                return
            visiting.add(block_id)
            for dependency in edges[block_id]:
                visit(dependency)
            visiting.remove(block_id)
            visited.add(block_id)

        for block_id in edges:
            visit(block_id)
        self.assertEqual(set(blocks), visited)

    def test_declared_layers_place_every_dependency_earlier(self) -> None:
        section = self.text.split(
            "## 3. Micro Block Dependency Order",
            1,
        )[1].split("## 4. Micro Blocks", 1)[0]
        layers: dict[str, int] = {}
        current_layer: int | None = None
        for raw_line in section.splitlines():
            line = raw_line.strip()
            match = LAYER_PATTERN.match(line)
            if match:
                current_layer = int(match.group("layer"))
                ids = ID_PATTERN.findall(match.group("ids"))
            elif current_layer is not None and line.startswith("M-B"):
                ids = ID_PATTERN.findall(line)
            else:
                continue
            for block_id in ids:
                self.assertNotIn(block_id, layers)
                layers[block_id] = current_layer

        blocks = self._blocks()
        self.assertEqual(set(blocks), set(layers))
        for block_id, body in blocks.items():
            match = PRECONDITION_PATTERN.search(body)
            assert match is not None
            for dependency in ID_PATTERN.findall(match.group("value")):
                self.assertLess(
                    layers[dependency],
                    layers[block_id],
                    f"{dependency} must precede {block_id}",
                )

    def test_all_declared_phase_four_core_targets_exist(self) -> None:
        targets = (
            "permission_spike.py",
            "worker_runner.py",
            "run_loop.py",
            "orca_loop/bootstrap.py",
            "orca_loop/config.py",
            "orca_loop/contracts.py",
            "orca_loop/coordinator.py",
            "orca_loop/dispatcher.py",
            "orca_loop/escalation.py",
            "orca_loop/generation.py",
            "orca_loop/guards.py",
            "orca_loop/ledger.py",
            "orca_loop/locking.py",
            "orca_loop/machine.py",
            "orca_loop/models.py",
            "orca_loop/orca_client.py",
            "orca_loop/profiles.py",
            "orca_loop/roles.py",
            "orca_loop/snapshot.py",
            "orca_loop/testrunner.py",
            "orca_loop/transport.py",
            "orca_loop/workspace.py",
            "tests/test_e2e_orca.py",
        )
        missing = [
            target for target in targets if not (self.root / target).is_file()
        ]
        self.assertEqual([], missing)
