"""Read-only model dashboard: workspace observations and separate artifact evidence."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Date, Integer, case, cast, func, or_, select

from app.core.config import settings
from app.models import AnalyticsHistoryState, InferenceRecord, Invoice, Payment, RecoveryAction, RecoveryCase
from app.models.enums import InvoiceStatus, PaymentStatus, RecoveryActionStatus

ARTIFACTS = Path(__file__).resolve().parents[1] / "ml/artifacts"
ROOT = Path(__file__).resolve().parents[3]


def read_report(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def research_evidence():
    """Only aggregate metrics leave the server; never source rows or local paths."""
    manifest = read_report(ARTIFACTS / "b2b_v2/champion_manifest.json")
    policy = read_report(ARTIFACTS / "b2b_v2/calibration/serving_calibration_policy.json")
    platt = read_report(ARTIFACTS / "b2b_v2/calibration/platt_temporal_audit.json")
    lightgbm = read_report(ARTIFACTS / "b2b_v2/lightgbm_challenge/calibration_audit.json")
    risk = read_report(ROOT / "docs/evaluations/model-risk-v1.json")
    targets = []
    for target in ("late_payment", "delay_over_7d", "delay_over_15d", "days_late_if_late_mean", "days_late_p80"):
        metadata = read_report(ARTIFACTS / f"b2b_v2/models/{target}/metadata.json")
        # The older late_payment Platt audit describes the superseded champion.
        audit = (lightgbm if target == "late_payment" else platt).get(target, {})
        targets.append({"target": target, "champion": manifest.get(target, {}).get("model"),
            "raw": audit.get("raw"), "calibrated": audit.get("platt"),
            "policy": policy.get(target, {}).get("serving_mode"),
            "experimental": "EXPERIMENTAL" in manifest.get(target, {}).get("status", ""),
            "validation": metadata.get("temporal_backtest"),
            "test_status": "Retrospective test" if target == "late_payment" and risk.get("test_status") else
                "Not evaluated" if metadata.get("test_used") is False else "Not recorded"})
    manifests = risk.get("inventory", {}).get("processed_dataset_manifests", {})
    bpi = manifests.get("bpi_invoice_features_manifest.json", {})
    final = bpi.get("final_dataset", {})
    # A cohort curve is not inferred from horizon averages or single-case predictions.
    checks = []
    for domain in ("b2b_v2", "bpi_survival"):
        pins = read_report(ARTIFACTS / domain / "runtime_integrity.json").get("files", {})
        for name, expected in pins.items():
            path = ARTIFACTS / domain / name
            checks.append(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    return {"targets": targets,
        "historical_rows": manifests.get("b2b_primary_features_manifest.json", {}).get("observation_horizon", {}).get("rows_before"),
        "test_disclosure": risk.get("test_status") or "Held-out test status unavailable. These calibration results are development evidence.",
        "artifacts": {"joblib": len(list(ARTIFACTS.rglob("*.joblib"))), "csv": len(list(ARTIFACTS.rglob("*.csv"))),
            "checksum_matches": sum(checks), "checksum_total": len(checks), "licence": "Not verified"},
        "bpi": {"events": None, "cases": bpi.get("population_audit", {}).get("all_cases"),
            "modelled_rows": final.get("rows"), "vendors": final.get("vendors"),
            "receipt_never_cleared": None,
            "cleared_after_45d": final.get("target_positive_rate"),
            "negative_timings": bpi.get("target_audit", {}).get("impossible_negative_clearing_rows_removed"),
            "curve": None, "curve_status": "Cohort survival curve unavailable in the verified evaluation artifacts."}}


def count_if(condition):
    return func.count().filter(condition)


async def live_evidence(db, identity):
    scope = Invoice.company_id.in_(identity.company_ids)
    eligible = Invoice.status.notin_([InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF])
    scored = func.coalesce(Invoice.late_payment_probability.between(0, 1)
        & Invoice.payment_risk_scored_at.is_not(None) & Invoice.payment_risk_model_version.is_not(None), False)
    open_invoice = (Invoice.amount_paid < Invoice.amount_total) & eligible
    total, scored_count, open_count, open_scored, last_score, explanations = (await db.execute(select(
        func.count(), count_if(scored), count_if(open_invoice), count_if(open_invoice & scored),
        func.max(Invoice.payment_risk_scored_at).filter(scored),
        count_if(scored & Invoice.payment_risk_drivers.is_not(None))
    ).where(scope, eligible))).one()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)
    inference_scope = or_(InferenceRecord.company_id.in_(identity.company_ids),
        InferenceRecord.company_id.is_(None) & (InferenceRecord.workspace_id == identity.workspace_id))
    telemetry = (await db.execute(select(func.count(),
        count_if(InferenceRecord.status.in_(["FAILED", "CANCELLED"])),
        func.percentile_cont(0.5).within_group(InferenceRecord.latency_ms),
        func.percentile_cont(0.95).within_group(InferenceRecord.latency_ms)
    ).where(inference_scope, InferenceRecord.model_kind == "b2b", InferenceRecord.source != "legacy_snapshot",
        InferenceRecord.started_at >= start, InferenceRecord.started_at < now))).one()
    coverage_start = await db.scalar(select(AnalyticsHistoryState.started_at).where(AnalyticsHistoryState.name == "inference_records"))
    invoice_ids = select(Invoice.id).where(scope, eligible)
    # First full settlement, with reconciled successful payments in invoice currency.
    payments = select(Payment.invoice_id, Payment.payment_date,
        func.sum(Payment.amount).over(partition_by=Payment.invoice_id, order_by=(Payment.payment_date, Payment.id)).label("running"),
        func.sum(Payment.amount).over(partition_by=Payment.invoice_id).label("total")
    ).join(Invoice, Invoice.id == Payment.invoice_id).where(Payment.invoice_id.in_(invoice_ids),
        Payment.status == PaymentStatus.SUCCESS, Payment.currency == Invoice.currency, Payment.payment_date <= now).subquery()
    settled = select(payments.c.invoice_id, func.min(payments.c.payment_date).label("at"))
    settled = settled.join(Invoice, Invoice.id == payments.c.invoice_id).where(
        payments.c.running >= Invoice.amount_total, payments.c.total == Invoice.amount_paid
    ).group_by(payments.c.invoice_id).subquery()
    adjusted_due = Invoice.due_date.op("+")(case((func.extract("isodow", Invoice.due_date) == 6, 2),
        (func.extract("isodow", Invoice.due_date) == 7, 1), else_=0))
    late = cast(func.timezone(settings.business_timezone, settled.c.at), Date) > adjusted_due
    band = func.least(cast(func.floor(Invoice.late_payment_probability * 5), Integer), 4)
    rows = (await db.execute(select(band.label("band"), func.count(), count_if(late)).select_from(Invoice)
        .join(settled, settled.c.invoice_id == Invoice.id).where(scope, eligible, scored,
            Invoice.status == InvoiceStatus.PAID, Invoice.amount_paid >= Invoice.amount_total)
        .group_by(band))).all()
    bands = [{"count": 0, "late_rate": None} for _ in range(5)]
    for index, count, late_count in rows:
        bands[index] = {"count": count, "late_rate": late_count / count}
    cases = await db.scalar(select(func.count()).select_from(RecoveryCase).where(RecoveryCase.invoice_id.in_(select(Invoice.id).where(scope)), RecoveryCase.company_id.in_(identity.company_ids)))
    approvals = await db.scalar(select(func.count()).select_from(RecoveryAction).join(RecoveryCase,
        RecoveryCase.id == RecoveryAction.recovery_case_id).where(RecoveryCase.invoice_id.in_(select(Invoice.id).where(scope)),
            RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL, RecoveryCase.company_id.in_(identity.company_ids)))
    return {"coverage": {"eligible": total, "scored": scored_count, "unscored": total - scored_count,
            "open": open_count, "open_scored": open_scored, "reasons": None},
        "last_scored_at": last_score, "explanations_stored": explanations,
        "health": {"requests": telemetry[0] if coverage_start or telemetry[0] else None,
            "failures": telemetry[1] if coverage_start or telemetry[0] else None,
            "p50_ms": telemetry[2], "p95_ms": telemetry[3], "recording_started_at": coverage_start,
            "complete_interval": coverage_start is not None and coverage_start <= start},
        "bands": bands, "settled_count": sum(b["count"] for b in bands),
        "outcome_note": "Stored latest scores; retrospective settlements, not prospective validation. Weekend-adjusted due dates. Mixed stored versions may be present.",
        "navigation": {"cases": cases, "approvals": approvals}}
