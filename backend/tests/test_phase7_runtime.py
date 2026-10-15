import asyncio
import unittest

from unittest.mock import patch

from app.agents.phase7_runtime import (
    Route,
    choose_route,
    run_routed,
)


INVOICE = {
    "days_overdue": 12,
    "amount_total": 150000,
}

CUSTOMER = {
    "num_prior_late_payments": 2,
}


class RoutingTests(unittest.TestCase):

    def test_disabled_compatibility(self):
        with patch.dict(
            "os.environ",
            {"INVOICEIQ_PHASE7_ROUTING": "0"},
        ):
            route = choose_route(
                "diagnose",
                INVOICE,
                CUSTOMER,
                "gemini",
            )

            self.assertEqual(route.provider, "legacy")

    def test_double_opt_in(self):
        with patch.dict(
            "os.environ",
            {
                "INVOICEIQ_PHASE7_ROUTING": "1",
                "INVOICEIQ_PHASE7_REMOTE": "0",
            },
        ):
            self.assertEqual(
                choose_route(
                    "recommend",
                    INVOICE,
                    CUSTOMER,
                    "gemini",
                ).provider,
                "fallback",
            )

        with patch.dict(
            "os.environ",
            {
                "INVOICEIQ_PHASE7_ROUTING": "1",
                "INVOICEIQ_PHASE7_REMOTE": "1",
            },
        ):
            self.assertEqual(
                choose_route(
                    "recommend",
                    INVOICE,
                    CUSTOMER,
                    "gemini",
                ).provider,
                "gemini",
            )

            self.assertEqual(
                choose_route(
                    "diagnose",
                    INVOICE,
                    CUSTOMER,
                    "groq",
                ).provider,
                "fallback",
            )

    def test_high_value_local(self):
        with patch.dict(
            "os.environ",
            {
                "INVOICEIQ_PHASE7_ROUTING": "1",
                "INVOICEIQ_PHASE7_REMOTE": "1",
            },
        ):
            route = choose_route(
                "diagnose",
                {**INVOICE, "amount_total": 2000000},
                CUSTOMER,
                "openai",
            )

            self.assertEqual(route.provider, "fallback")

    def test_invalid_evidence_local(self):
        with patch.dict(
            "os.environ",
            {
                "INVOICEIQ_PHASE7_ROUTING": "1",
                "INVOICEIQ_PHASE7_REMOTE": "1",
            },
        ):
            route = choose_route(
                "diagnose",
                {**INVOICE, "days_overdue": -2},
                CUSTOMER,
                "gemini",
            )

            self.assertEqual(route.provider, "fallback")

    def test_forbidden_stage(self):
        with self.assertRaises(ValueError):
            choose_route(
                "execute_action",
                INVOICE,
                CUSTOMER,
                "gemini",
            )

    def test_fallback_does_not_call_remote(self):
        async def forbidden():
            raise AssertionError("Unexpected remote call")

        result = asyncio.run(
            run_routed(
                stage="diagnose",
                route=Route("fallback", "test"),
                local=lambda: "safe",
                original=forbidden,
                fallback_model="rule-based-fallback",
            )
        )

        self.assertEqual(
            result,
            ("safe", "rule-based-fallback"),
        )

    def test_provider_exception_falls_back(self):
        async def failed():
            raise RuntimeError("Test provider failure")

        with self.assertLogs("app.phase7"):
            result = asyncio.run(
                run_routed(
                    stage="diagnose",
                    route=Route("gemini", "test"),
                    local=lambda: "safe",
                    original=failed,
                    fallback_model="rule-based-fallback",
                )
            )

        self.assertEqual(
            result,
            ("safe", "rule-based-fallback"),
        )
