from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.payment_risk_scoring as scoring


@pytest.fixture(autouse=True)
def unit_telemetry_boundary(monkeypatch):
    # These are isolated scoring unit tests; database telemetry is integration-tested separately.
    async def tracked(operation, **kwargs):
        return await operation()
    monkeypatch.setattr(scoring, "tracked_inference", tracked)


MODEL = "random_forest"
VERSION = "random_forest:d42e3f72f4cf"


class FakeFeatures:
    def to_row(self):
        return {
            "amount": 1000.0,
            "payment_term": 30.0,
        }


class FakeSession:
    def __init__(self):
        self.flush = AsyncMock()

    @asynccontextmanager
    async def begin_nested(self):
        yield


def make_invoice(
    *,
    probability=None,
    model=None,
    version=None,
):
    return SimpleNamespace(
        id=None, company_id=None, currency="INR",
        invoice_number="INV-HARDENING-001",
        late_payment_probability=probability,
        payment_risk_model=model,
        payment_risk_model_version=version,
        payment_risk_scored_at=None,
        payment_risk_features=None,
        payment_risk_drivers=None,
    )


@pytest.mark.asyncio
async def test_same_model_version_is_idempotent(
    monkeypatch,
):
    monkeypatch.setenv(
        "PAYMENT_RISK_MODEL",
        MODEL,
    )

    monkeypatch.setattr(
        scoring,
        "get_payment_risk_model_version",
        lambda _: VERSION,
    )

    async def should_not_build(*args, **kwargs):
        raise AssertionError(
            "features should not be rebuilt"
        )

    monkeypatch.setattr(
        scoring,
        "build_payment_risk_features",
        should_not_build,
    )

    invoice = make_invoice(
        probability=Decimal("0.420000"),
        model=MODEL,
        version=VERSION,
    )

    result = await scoring.ensure_payment_risk_score(
        FakeSession(),
        invoice,
    )

    assert result is True
    assert invoice.late_payment_probability == Decimal(
        "0.420000"
    )


@pytest.mark.asyncio
async def test_changed_model_version_forces_rescore(
    monkeypatch,
):
    monkeypatch.setenv(
        "PAYMENT_RISK_MODEL",
        MODEL,
    )

    monkeypatch.setattr(
        scoring,
        "get_payment_risk_model_version",
        lambda _: VERSION,
    )

    async def fake_features(*args, **kwargs):
        return FakeFeatures()

    monkeypatch.setattr(
        scoring,
        "build_payment_risk_features",
        fake_features,
    )

    prediction = SimpleNamespace(
        late_payment_probability=0.73,
        model_name=MODEL,
        model_version=VERSION,
        contributions=[],
    )

    monkeypatch.setattr(
        scoring,
        "predict_payment_risk",
        lambda *args, **kwargs: prediction,
    )

    invoice = make_invoice(
        probability=Decimal("0.200000"),
        model=MODEL,
        version="random_forest:OLDVERSION",
    )

    session = FakeSession()

    result = await scoring.ensure_payment_risk_score(
        session,
        invoice,
    )

    assert result is True
    assert invoice.late_payment_probability == Decimal(
        "0.730000"
    )
    assert invoice.payment_risk_model == MODEL
    assert invoice.payment_risk_model_version == VERSION
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_scoring_error_is_fail_safe(
    monkeypatch,
):
    monkeypatch.setenv(
        "PAYMENT_RISK_MODEL",
        MODEL,
    )

    monkeypatch.setattr(
        scoring,
        "get_payment_risk_model_version",
        lambda _: VERSION,
    )

    async def explode(*args, **kwargs):
        raise RuntimeError(
            "simulated scoring failure"
        )

    monkeypatch.setattr(
        scoring,
        "build_payment_risk_features",
        explode,
    )

    invoice = make_invoice()

    result = await scoring.ensure_payment_risk_score(
        FakeSession(),
        invoice,
    )

    assert result is False
    assert invoice.late_payment_probability is None
    assert invoice.payment_risk_model is None
    assert invoice.payment_risk_model_version is None
