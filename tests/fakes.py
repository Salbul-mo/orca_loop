from __future__ import annotations

import json
from collections.abc import Callable

from orca_loop.models import OrcaResponse


class FakeOrcaClient:
    def __init__(
        self,
        handler: Callable[[tuple[str, ...], int], dict[str, object]],
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.executable = "C:\\fake\\orca.exe"

    def call(
        self,
        argv: tuple[str, ...],
        *,
        timeout_ms: int,
    ) -> OrcaResponse:
        self.calls.append((argv, timeout_ms))
        result = self.handler(argv, timeout_ms)
        wrapper = {"ok": True, "result": result}
        return OrcaResponse(
            result_json=json.dumps(result),
            stdout=json.dumps(wrapper),
            stderr="",
        )
