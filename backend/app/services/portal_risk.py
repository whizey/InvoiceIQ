"""Posting-time B2B inference over one isolated portal ledger.

Only observations available before each invoice posting enter its features.
Stored scores carry the verified champion version and artifact fingerprint.
"""
from collections import defaultdict
from decimal import Decimal
from math import isfinite

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.ml.champion_serving import champion_predict, champion_predict_many, verified_contract
from app.ml.payment_risk_model import PaymentRiskArtifactError
from app.services.payment_risk_context import PriorInvoiceSnapshot, compute_payment_risk_features
from app.models.portal import PortalInvoice, PortalPayment


def provenance(contract):
    return f"{contract['version']}:{contract['artifact_sha256'][:12]}"


async def workspace_features(db, workspace_id):
    invoices = (await db.scalars(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace_id).order_by(
            PortalInvoice.posting_date, PortalInvoice.invoice_number).limit(2001))).all()
    if len(invoices) > 2000:
        raise HTTPException(422, "Score a portfolio of at most 2,000 invoices")
    payments = (await db.scalars(select(PortalPayment).where(
        PortalPayment.workspace_id == workspace_id).order_by(
            PortalPayment.paid_on, PortalPayment.id).limit(5001))).all()
    if len(payments) > 5000:
        raise HTTPException(422, "Payment ledger exceeds the scoring limit")
    by_invoice = defaultdict(list)
    invoice_ids = {invoice.id for invoice in invoices}
    for payment in payments:
        if payment.invoice_id not in invoice_ids:
            raise HTTPException(503, "Payment belongs to a different workspace")
        by_invoice[payment.invoice_id].append(payment)
    histories = defaultdict(list)
    result = []
    for invoice in invoices:
        movements = by_invoice[invoice.id]
        if sum((payment.amount for payment in movements), Decimal(0)) != invoice.amount_paid:
            raise HTTPException(503, "Dated payment ledger does not reconcile")
        key = invoice.counterparty.casefold()
        feature = compute_payment_risk_features(
            amount=float(invoice.amount_total), posting_date=invoice.posting_date,
            payment_term=(invoice.due_date - invoice.issue_date).days,
            history=histories[key],
        )
        result.append((invoice, feature))
        clearing_date = None
        running = Decimal(0)
        for payment in movements:
            running += payment.amount
            if running >= invoice.amount_total:
                clearing_date = payment.paid_on
                break
        histories[key].append(PriorInvoiceSnapshot(
            invoice_number=invoice.invoice_number, amount=float(invoice.amount_total),
            posting_date=invoice.posting_date, due_date=invoice.due_date,
            clearing_date=clearing_date,
        ))
    return result


async def score_workspace(db, workspace_id):
    rows = await workspace_features(db, workspace_id)
    if not rows:
        return {"scored": 0, "model_version": None}
    try:
        contract = await run_in_threadpool(verified_contract)
        predictions = await run_in_threadpool(champion_predict_many, [feature for _, feature in rows])
    except PaymentRiskArtifactError as exc:
        raise HTTPException(503, "Verified model artifact unavailable") from exc
    expected = provenance(contract)
    for (invoice, _), prediction in zip(rows, predictions, strict=True):
        if prediction.model_version != contract["version"] or prediction.artifact_sha256 != contract["artifact_sha256"]:
            raise HTTPException(503, "Model version changed while scoring")
        invoice.risk_probability = Decimal(str(prediction.late_payment_probability))
        invoice.risk_provenance = expected
    await db.flush()
    return {"scored": len(rows), "model_version": contract["version"]}


async def explain_invoice(db, workspace_id, invoice_id):
    rows = await workspace_features(db, workspace_id)
    match = next(((invoice, features) for invoice, features in rows if invoice.id == invoice_id), None)
    if match is None:
        raise HTTPException(404, "Invoice not found")
    invoice, features = match
    if invoice.risk_probability is None:
        raise HTTPException(409, "Invoice has not been scored")
    try:
        contract = await run_in_threadpool(verified_contract)
        if invoice.risk_provenance != provenance(contract):
            raise HTTPException(409, "Stored score uses a different model version")
        prediction = await run_in_threadpool(champion_predict, features)
    except PaymentRiskArtifactError as exc:
        raise HTTPException(503, "Verified model artifact unavailable") from exc
    if abs(Decimal(str(prediction.late_payment_probability)) - invoice.risk_probability) > Decimal("0.000001"):
        raise HTTPException(409, "Stored score no longer matches the invoice history; rescore this workspace")
    return {
        "invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number,
        "counterparty": invoice.counterparty,
        "probability": str(invoice.risk_probability),
        "model_version": contract["version"],
        "artifact_sha256": contract["artifact_sha256"],
        "explanation_method": "LightGBM native TreeSHAP",
        "explanation_base_value": prediction.explanation_base_value,
        "contributions": [vars_for_contribution(item) for item in prediction.contributions],
    }


def vars_for_contribution(item):
    value = item.value
    if isinstance(value, (float, int)) and not isfinite(value):
        value = None
    return {"feature": item.feature, "label": item.label, "value": value,
            "shap_value": item.shap_value, "direction": item.direction}
