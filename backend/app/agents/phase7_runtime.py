"""Opt-in agent routing and metadata-only operational tracing.

The existing deterministic policy engine remains the only
authorization gate for recovery actions.
"""

import logging
import os
import time

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Awaitable, TypeVar

LOG = logging.getLogger("app.phase7")
T = TypeVar("T")

VERSION = "phase7-v1"


def enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {
        "1", "true", "yes"
    }


def nonnegative(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        number = Decimal(str(value))

        if number.is_finite() and number >= 0:
            return number

    except (InvalidOperation, ValueError, TypeError):
        pass

    return None


@dataclass(frozen=True)
class Route:
    provider: str
    reason: str
    version: str = VERSION


def choose_route(
    stage: str,
    invoice: dict,
    customer: dict,
    provider: str,
) -> Route:

    if stage not in {"diagnose", "recommend"}:
        raise ValueError(
            "Only diagnosis/recommendation may use LLM routing"
        )

    if not enabled("INVOICEIQ_PHASE7_ROUTING"):
        return Route("legacy", "disabled")

    days = nonnegative(invoice.get("days_overdue"))
    amount = nonnegative(invoice.get("amount_total"))
    late = nonnegative(
        customer.get("num_prior_late_payments")
    )

    if days is None or amount is None or late is None:
        return Route("fallback", "incomplete_evidence")

    if amount >= 1_000_000:
        return Route("fallback", "high_value_local_only")

    promise = customer.get("promise_to_pay")

    if (
        isinstance(promise, dict)
        and promise.get("status") == "BROKEN"
    ):
        return Route(
            "fallback",
            "broken_promise_local_only",
        )

    if days <= 7 and late == 0:
        return Route("fallback", "simple_case")

    if not enabled("INVOICEIQ_PHASE7_REMOTE"):
        return Route("fallback", "remote_not_enabled")

    if provider not in {"gemini", "openai"}:
        return Route("fallback", "unsupported_provider")

    return Route(provider, "remote_opt_in")


async def run_routed(
    *,
    stage: str,
    route: Route,
    local: Callable[[], T],
    original: Callable[[], Awaitable[tuple[T, str]]],
    fallback_model: str,
):
    started = time.monotonic()

    model = fallback_model
    outcome = "ok"

    try:
        if route.provider == "fallback":
            result = local()

        else:
            try:
                result, model = await original()

            except Exception:
                LOG.warning(
                    "agent provider failed; local fallback used",
                    extra={"phase7_stage": stage},
                )

                result = local()
                model = fallback_model

        return result, model

    except Exception:
        outcome = "error"
        raise

    finally:
        LOG.info(
            "agent_stage_completed",
            extra={
                "phase7_stage": stage,
                "phase7_route": route.provider,
                "phase7_reason": route.reason,
                "phase7_model": model,
                "phase7_fallback": (
                    model == fallback_model
                ),
                "phase7_outcome": outcome,
                "phase7_duration_ms": round(
                    (time.monotonic() - started) * 1000,
                    2,
                ),
                "phase7_version": route.version,
            },
        )
