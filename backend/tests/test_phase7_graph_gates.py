"""LangGraph branching tests without database writes."""

import asyncio
import unittest

from unittest.mock import AsyncMock, patch

from app.agents import graph
from app.models.enums import PolicyDecisionResult


class GraphGateTests(unittest.TestCase):

    def run_graph(self, decision, terminal=False):
        names = [
            "check_terminal_node",
            "load_customer_context_node",
            "calculate_recovery_risk_node",
            "diagnose_case_node",
            "recommend_intervention_node",
            "policy_check_node",
            "execute_action_node",
            "record_outcome_node",
        ]

        values = [
            {"terminal": terminal},
            {
                "invoice_context": {},
                "customer_context": {},
            },
            {'risk_score': 0.35, 'risk_level': 'MEDIUM', 'recovery_probability': 0.65, 'risk_drivers': []},
            {'diagnosis': {'schema_version': '1', 'financial_claims': [], 'diagnosis': 'Invoice requires follow-up', 'reason': 'Payment remains outstanding.', 'recommended_priority': 'medium'}, 'knowledge_evidence': []},
            {
                "recommended_action": "SEND_EMAIL",
                "reminder_count": 0,
                "recommendation_rationale": "Send a reminder.",
            },
            {
                "policy_decision": decision,
                "action_id": (
                    "00000000-0000-0000-0000-000000000001"
                ),
                "final_action": "SEND_EMAIL",
                "policy_reason": "Test approval.",
            },
            {"action_result": {"simulated": True}},
            {'case_status': 'OPEN', 'outcome_summary': 'Simulated reminder.'},
        ]

        contexts = []

        try:
            for name, value in zip(names, values):
                context = patch.object(
                    graph,
                    name,
                    new_callable=AsyncMock,
                    return_value=value,
                )

                contexts.append(context)
                context.start()

            asyncio.run(
                graph._build_graph(object()).ainvoke({
                    "case_id": (
                        "00000000-0000-0000-0000-000000000001"
                    )
                })
            )

            return {
                name: getattr(graph, name).await_count
                for name in names
            }

        finally:
            for context in reversed(contexts):
                context.stop()

    def test_human_review_blocks_execution(self):
        calls = self.run_graph(
            PolicyDecisionResult.REQUIRES_HUMAN_REVIEW.value
        )

        self.assertEqual(calls["policy_check_node"], 1)
        self.assertEqual(calls["execute_action_node"], 0)
        self.assertEqual(calls["record_outcome_node"], 0)

    def test_approved_enters_execution(self):
        calls = self.run_graph(
            PolicyDecisionResult.APPROVED.value
        )

        self.assertEqual(calls["execute_action_node"], 1)
        self.assertEqual(calls["record_outcome_node"], 1)

    def test_terminal_invoice_stops(self):
        calls = self.run_graph(
            PolicyDecisionResult.APPROVED.value,
            terminal=True,
        )

        self.assertEqual(calls["diagnose_case_node"], 0)
        self.assertEqual(calls["execute_action_node"], 0)
