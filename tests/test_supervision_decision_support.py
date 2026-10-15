from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orca_loop.escalation import create_gate
from orca_loop.local_runtime import LocalCoordinatorClient
from orca_loop.models import (
    DecisionReport,
    EscalationCode,
    EscalationTrigger,
    GateKind,
)
from run_loop import _gate_decision_support


class SupervisionDecisionSupportTest(unittest.TestCase):
    def test_e03_support_explains_problem_options_and_recommendation(self) -> None:
        trigger = EscalationTrigger(
            EscalationCode.E03,
            "고객 Web Server API 계약을 변경할 권한이 확인되지 않았습니다.",
            ("docs/Web Server_API v4.html",),
            "e03-contract-authority",
        )
        controller = SimpleNamespace(
            state=SimpleNamespace(
                pending_escalations=(trigger,),
                blocked_from_state=None,
                history=(),
                test_gate_status=None,
            )
        )
        support = _gate_decision_support(
            controller,
            GateKind.ESCALATION,
            ("merge", "reject", "revise_design"),
            None,
        )

        self.assertIn("계약을 변경할 권한", support["problem"])
        self.assertIn("사용자의 명시적 결정", support["whyDecisionNeeded"])
        self.assertEqual(["docs/Web Server_API v4.html"], support["evidence"])
        self.assertEqual("revise_design", support["recommendedDecision"])
        self.assertIn("변경 권한", support["recommendation"])
        self.assertEqual(
            ["merge", "reject", "revise_design"],
            [item["decision"] for item in support["options"]],
        )
        self.assertTrue(
            all(item["consequence"] for item in support["options"])
        )

    def test_local_gate_publishes_structured_decision_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = LocalCoordinatorClient(
                cwd=root,
                run_id="decision-support-run",
                coordinator_handle="local-coordinator",
            )
            with mock.patch.object(
                client,
                "_publish_supervision",
                return_value=True,
            ) as publisher:
                task_id = json.loads(
                    client.call(
                        (
                            "orchestration",
                            "task-create",
                            "--task-title",
                            "decision",
                            "--display-name",
                            "decision",
                            "--spec",
                            "human decision",
                        ),
                        timeout_ms=1000,
                    ).result_json
                )["task"]["id"]

                report_path = root / "user-decision.md"
                report_path.write_text("# decision", encoding="utf-8")
                support = {
                    "problem": "외부 계약 변경 권한이 불명확합니다.",
                    "whyDecisionNeeded": "자동 확정이 금지된 경계입니다.",
                    "evidence": ["docs/api.html"],
                    "options": [
                        {
                            "decision": "revise_design",
                            "title": "설계 수정 요청",
                            "description": "외부 계약을 유지하도록 수정합니다.",
                            "consequence": "Planner 단계로 돌아갑니다.",
                        },
                        {
                            "decision": "reject",
                            "title": "승인하지 않음",
                            "description": "현재 방향을 승인하지 않습니다.",
                            "consequence": "현재 run을 종료합니다.",
                        },
                    ],
                    "recommendedDecision": "revise_design",
                    "recommendation": "설계 수정 요청을 권장합니다.",
                    "approvalEffect": "현재 방향으로 계속 진행합니다.",
                    "noDecisionEffect": "run은 차단 상태를 유지합니다.",
                }

                binding = create_gate(
                    client,
                    task_id=task_id,
                    report=DecisionReport(
                        path=report_path,
                        digest="sha256:" + "a" * 64,
                        finding_ids=(),
                    ),
                    gate_kind=GateKind.ESCALATION,
                    question="외부 계약 경계를 결정하세요.",
                    options=("revise_design", "reject"),
                    decision_support=support,
                    timeout_ms=1000,
                )

            notification_path = (
                client.notification_dir / f"{binding.gate_id}.json"
            )
            notification = json.loads(
                notification_path.read_text(encoding="utf-8")
            )
            self.assertEqual(support, notification["decisionSupport"])
            self.assertEqual(str(report_path), notification["reportPath"])
            published = publisher.call_args.args[0]
            self.assertEqual(support, published["decisionSupport"])
            self.assertEqual(str(report_path), published["reportPath"])

    def test_publish_supervision_includes_resolved_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = LocalCoordinatorClient(
                cwd=Path(temp),
                run_id="resolved-supervision-run",
                coordinator_handle="local-coordinator",
            )
            resolution = {
                "decision": "merge",
                "decision_note": "Approved by the supervising authority.",
                "affected_acceptance_criteria": [],
                "affected_finding_ids": [],
                "report_digest": "sha256:" + "b" * 64,
            }
            notification = {
                "gateId": "local-gate-resolved-1",
                "runId": "resolved-supervision-run",
                "question": "Continue?",
                "options": ["merge", "reject"],
                "reportPath": None,
                "decisionSupport": None,
                "status": "resolved",
                "resolution": resolution,
            }
            response = mock.MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            with mock.patch.dict(
                os.environ,
                {
                    "ORCA_WEB_AGENT_TOKEN": "x" * 32,
                    "ORCA_WEB_AGENT_BRIDGE_URL": "http://127.0.0.1:8765",
                },
                clear=False,
            ), mock.patch(
                "orca_loop.local_runtime.urllib.request.urlopen",
                return_value=response,
            ) as urlopen:
                self.assertTrue(client._publish_supervision(notification))

            request = urlopen.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual("resolved", payload["status"])
            self.assertEqual(resolution, payload["resolution"])



if __name__ == "__main__":
    unittest.main()
