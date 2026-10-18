"""Read-only, scoped operational analytics. Each aggregate has its own date axis.

No fanout joins: actions, decisions, payments and cases aggregate independently.
Monetary totals never mix currencies. Balances/statuses are CURRENT cohort
state, not reconstructed historical balances. Inference attempts have durable history; legacy scores retain explicit provenance.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Date, String, Numeric, Boolean, cast, case, func, select, true, column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import selectinload, aliased

from app.core.config import settings
from app.core.analytics_auth import AnalyticsIdentity
from app.services.persisted_predictions import valid_score, stored_prediction_report
from app.models import AgentDecision, AuditLog, Company, Invoice, Payment, PolicyDecision, RecoveryAction, RecoveryCase
from app.models.enums import AgentDecisionStage, InvoiceStatus, PaymentStatus, RecoveryActionStatus, RecoveryCaseStatus
from app.schemas.analytics import ActivityRecord, AnalyticsBucket, AnalyticsOut, AnalyticsRange, AnalyticsSummary, InferenceRecordOut, FinancialCohortMetrics
from app.schemas.invoice import InvoiceOut
from app.models import InferenceRecord, DatasetImport
from app.services.analytics_history import historical_snapshot, inference_telemetry, inference_scope

from app.services.analytics_reconciliation import reconciliation_for_interval

SEMANTICS = {
    "invoice_cohort": "Posting date, falling back to issue date; inclusive calendar dates.",
    "outstanding_amount": "Current unpaid balance of selected invoice cohort; not historical as-of.",
    "overdue_amount": "Current unpaid cohort balance due before selected end date; excludes cancelled/written-off invoices. Not historical as-of.",
    "cases": "Cases opened during interval; displayed status is current, not historical as-of.",
    "recovered_amount": "Successful stored payment events during interval, across scoped invoices; Payment events are not attributed to recovery actions; this reports payment collections, not causal recovery effectiveness.",
    "actions": "Actions proposed during interval; displayed execution/approval status is current.",
    "approvals": "Requests: held or reviewed actions proposed in interval. Decisions: review audit events in interval.",
    "investigations": "Persisted diagnoses created in interval; completion requires a recommendation with the same investigation identifier. Legacy decisions without identifiers are not assumed complete.",
    "predictions": "Recorded successful B2B inference completions in the invocation interval; retained separately from stored invoice-cohort scores.",
    "stored_predictions": "One trusted latest B2B score per invoice in the posting-date cohort, including imported batch scores. No request count or latency is inferred from these scores.",
    "historical": "Separate as-of snapshot from database-maintained state history, available only from the observed migration baseline; current cohort metrics remain explicitly current.",
    "inference_telemetry": "Actual model invocations since instrumentation began, including errors and latency, independent of financial transaction rollback. Cache hits and legacy score imports are not requests. B2B and BPI are separate; incomplete earlier interval coverage is explicit.",
    "risk_distribution": "Valid latest B2B lateness probabilities in invoice cohort: low <0.3, medium <0.7, high >=0.7; unscored separate.",
    "prediction_outcomes": "Persisted B2B prediction completions in interval with subsequent full settlement and complete successful payment histories reconciled to current paid balance. Counts are per prediction, including repeated forecasts, not unique invoices. Standalone feature-inspector predictions have no invoice ground truth. Lateness uses business-local settlement date and weekend-adjusted due date. No period-specific AUC is inferred.",
    "comparison": "Immediately preceding interval with equal number of calendar days; current cohort status limitations also apply.",
    "offline_evaluation": "Fixed provenance-linked B2B and BPI evaluation artifacts; independent of operational dates.",
}
ACTIVE = [RecoveryCaseStatus.OPEN, RecoveryCaseStatus.MONITORING, RecoveryCaseStatus.ESCALATED]


def _sum(expression):
    return func.coalesce(func.sum(expression), 0)


def _count_if(condition):
    return _sum(case((condition, 1), else_=0))



# Structured definitions are returned with every response, not inferred by charts.
METRIC_DEFINITIONS = {
    "invoiced": {"formula": "sum(amount_total)", "date_field": "coalesce(posting_date, issue_date)", "cohort": "scoped invoices in inclusive posting interval", "numerator": "invoice face amounts", "denominator": "invoice_count", "currency": "requested currency; never mixed", "tables": "invoices", "nulls": "posting date falls back to issue date", "as_of": "original dates preserved"},
    "collected": {"formula": "sum(successful payment.amount)", "date_field": "payment_date", "cohort": "all scoped invoices, independent of posting interval", "numerator": "observed successful payments", "denominator": "payment events", "currency": "requested currency", "tables": "payments,invoices", "nulls": "missing events contribute zero, never inferred recovery", "as_of": "half-open business-local timestamp interval"},
    "historical_outstanding": {"formula": "sum(max(amount_total - successful payments through cutoff,0))", "date_field": "posting_date and payment_date", "cohort": "selected reconciled public-source invoice cohort observable by cutoff", "numerator": "unpaid observable balance", "denominator": "observed_invoice_count", "currency": "requested currency", "tables": "invoices,payments,dataset_imports", "nulls": "unavailable without reconciled source history", "as_of": "min(end_date,dataset_as_of); future payments excluded"},
    "late_rate": {"formula": "late fully settled invoices / fully settled invoices", "date_field": "posting cohort; settlement through cutoff", "cohort": "observable fully settled invoices; prediction comparison uses only trusted scored subset", "numerator": "settlement date > weekend-adjusted due date", "denominator": "resolved_invoice_count", "currency": "not applicable", "tables": "invoices,payments", "nulls": "null with no observable settlements; outstanding outcomes censored", "as_of": "no future clearing; descriptive, retrospectively scored, not a temporal validation result"},
    "delay": {"formula": "average and median max(settlement_date - weekend-adjusted due_date,0)", "date_field": "posting cohort", "cohort": "observable fully settled invoices", "numerator": "nonnegative calendar days", "denominator": "resolved_invoice_count", "currency": "not applicable", "tables": "invoices,payments", "nulls": "null if no labels", "as_of": "settlement through cutoff only"},
    "stored_predictions": {"formula": "count invoices with trusted latest provenance", "date_field": "posting_date", "cohort": "invoice posting cohort", "numerator": "valid_score()", "denominator": "invoice_count", "currency": "requested currency", "tables": "invoices", "nulls": "invalid/missing provenance is unscored", "as_of": "retrospective model execution timestamp; not historical deployment"},
    "workflow": {"formula": "count existing cases/actions/decisions/audits", "date_field": "explicit workflow_date_axis: event timestamp or linked invoice posting date", "cohort": "scoped actual application records", "numerator": "record count", "denominator": "case_count or action_count as labelled", "currency": "not an observed recovery outcome", "tables": "recovery_cases,recovery_actions,agent_decisions,audit_logs", "nulls": "zero if no records", "as_of": "simulated workflow events, actual stored status; never original financial history"},
    "shap": {"formula": "mean(abs(persisted contribution)) per feature", "date_field": "invoice posting cohort", "cohort": "trusted valid scored invoices with validated TreeSHAP metadata", "numerator": "absolute log-odds contribution", "denominator": "shap_invoice_count", "currency": "log odds, not INR", "tables": "invoices", "nulls": "absent explanations excluded", "as_of": "retrospective model explanations; not causal effects"},
}


def _event_window(interval, timestamp, start, stop):
    return [] if interval.workflow_date_axis == "invoice" else [timestamp >= start, timestamp < stop]


def _period(column, grain):
    # Stable ISO start date permits exact drilldown for all supported grains.
    return func.to_char(func.date_trunc(grain, column), "YYYY-MM-DD")


async def _financial_metrics(db, interval, identity, imported, *, contract=None):
    from app.ml.champion_serving import verified_contract
    contract = contract if contract is not None else verified_contract()
    cutoff = min(interval.end_date, imported.dataset_as_of) if imported else interval.end_date
    result = FinancialCohortMetrics(cutoff=cutoff,
        reason="Not available: this cohort has no reconciled source payment history")
    scoped = [Invoice.company_id.in_(identity.company_ids), Invoice.currency == interval.currency,
        func.coalesce(Invoice.posting_date, Invoice.issue_date).between(interval.start_date, interval.end_date)]
    result.mean_predicted_probability = await db.scalar(select(func.avg(Invoice.late_payment_probability)).where(*scoped, valid_score(contract)))
    contributions = func.coalesce(Invoice.payment_risk_explanation_metadata["all_contributions"], Invoice.payment_risk_drivers)
    # Phase 1 batch imports store all drivers in the driver array; newer runtime
    # calls can retain a summary there and the full array in metadata. Neither is rewritten.
    feature_count = len(contract["metadata"]["features"])
    # Only full native explanations verified by the existing scoring runtime.
    explanation_ok = (valid_score(contract) & (Invoice.payment_risk_explanation_metadata["method"].astext == "LightGBM native TreeSHAP")
        & Invoice.payment_risk_explanation_metadata["base_value"].astext.is_not(None)
        & (func.jsonb_typeof(contributions) == "array")
        & (func.jsonb_array_length(contributions) == feature_count))
    result.shap_invoice_count = await db.scalar(select(func.count()).where(*scoped, explanation_ok))
    drivers = func.jsonb_array_elements(contributions).table_valued(column("value", JSONB)).lateral()
    shap_rows = (await db.execute(select(drivers.c.value["feature"].astext.label("feature"),
        func.avg(func.abs(cast(drivers.c.value["shap_value"].astext, Numeric))).label("mean_abs_shap"),
        func.count().label("contribution_count")).select_from(Invoice).join(drivers, true())
        .where(*scoped, explanation_ok).group_by("feature")
        .order_by(func.avg(func.abs(cast(drivers.c.value["shap_value"].astext, Numeric))).desc()))).mappings().all()
    result.shap_importance = [dict(row) for row in shap_rows]
    foreign = await db.scalar(select(func.count()).where(*scoped, ~Invoice.invoice_number.like("B2B-%")))
    if imported is None or foreign:
        return result
    result.reconstructable = True
    result.reason = "Reconstructed from original successful source payment events; no simulated recovery outcomes"
    # Public source payment timestamps are original clearing dates at UTC midnight.
    stop = datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), timezone.utc)
    # Only scan this workspace's posting cohort. Use first full settlement,
    # rather than any later top-up, for delay labels; exclude future payments.
    cohort_ids = select(Invoice.id).where(*scoped, Invoice.posting_date <= cutoff)
    cumulative = select(Payment.invoice_id.label("invoice_id"),
        Payment.payment_date.label("payment_date"), Payment.amount.label("amount"),
        func.sum(Payment.amount).over(partition_by=Payment.invoice_id,
            order_by=(Payment.payment_date, Payment.id), rows=(None, 0)).label("paid_to_date")).where(
        Payment.invoice_id.in_(cohort_ids), Payment.status == PaymentStatus.SUCCESS,
        Payment.currency == interval.currency, Payment.payment_date < stop).subquery()
    paid = select(cumulative.c.invoice_id, func.sum(cumulative.c.amount).label("paid"),
        func.min(case((cumulative.c.paid_to_date >= Invoice.amount_total,
            cumulative.c.payment_date), else_=None)).label("settled_at")).join(
        Invoice, Invoice.id == cumulative.c.invoice_id).group_by(cumulative.c.invoice_id).subquery()
    balance = func.greatest(Invoice.amount_total - func.coalesce(paid.c.paid, 0), 0)
    eligible = Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF])
    observable = [*scoped, Invoice.posting_date <= cutoff]
    overdue = eligible & (Invoice.due_date < cutoff) & (balance > 0)
    row = (await db.execute(select(func.count(), _sum(case((eligible, balance), else_=0)),
        _sum(case((overdue, balance), else_=0)), _count_if(overdue)).select_from(Invoice)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id).where(*observable))).one()
    result.observed_invoice_count, result.outstanding_amount, result.overdue_amount, result.overdue_invoice_count = row
    age = cast(cutoff, Date) - Invoice.due_date
    band = case((age <= 30, "1–30"), (age <= 60, "31–60"), (age <= 90, "61–90"), else_="91+")
    result.aging = [dict(row) for row in (await db.execute(select(band.label("bucket"), func.count().label("count"),
        _sum(balance).label("amount")).select_from(Invoice).outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .where(*observable, overdue).group_by(band))).mappings().all()]
    adjusted_due = Invoice.due_date.op("+")(case((func.extract("isodow", Invoice.due_date) == 6, 2),
        (func.extract("isodow", Invoice.due_date) == 7, 1), else_=0))
    delay = func.greatest(cast(func.timezone("UTC", paid.c.settled_at), Date) - adjusted_due, 0)
    labels = [*observable, paid.c.paid >= Invoice.amount_total, Invoice.amount_total > 0]
    row = (await db.execute(select(func.count(), _count_if(delay > 0), func.avg(delay),
        func.percentile_cont(0.5).within_group(delay)).select_from(Invoice).join(paid, paid.c.invoice_id == Invoice.id)
        .where(*labels))).one()
    result.resolved_invoice_count, result.late_invoice_count, result.average_delay_days, result.median_delay_days = row
    result.actual_late_rate = result.late_invoice_count / result.resolved_invoice_count if result.resolved_invoice_count else None
    period = _period(Invoice.posting_date, interval.grain)
    result.delay_series = [dict(row) for row in (await db.execute(select(period.label("period"), func.count().label("resolved_count"),
        func.avg(delay).label("average_delay_days"), func.avg(case((delay > 0, 1.0), else_=0.0)).label("actual_late_rate"),
        func.avg(Invoice.late_payment_probability).label("mean_predicted_probability"))
        .select_from(Invoice).join(paid, paid.c.invoice_id == Invoice.id).where(*labels, valid_score(contract))
        .group_by(period).order_by(period))).mappings().all()]
    return result


async def _summary(db, interval, identity, *, contract=None):
    start, stop = interval.timestamps((interval.source_timezone or settings.business_timezone))
    scoped = [Invoice.company_id.in_(identity.company_ids), Invoice.currency == interval.currency]
    invoice_ids = select(Invoice.id).where(*scoped)
    workflow_invoices = invoice_ids
    if interval.workflow_date_axis == "invoice":
        workflow_invoices = invoice_ids.where(func.coalesce(Invoice.posting_date, Invoice.issue_date).between(interval.start_date, interval.end_date))
    case_ids = select(RecoveryCase.id).where(RecoveryCase.invoice_id.in_(workflow_invoices))
    if interval.case_id is not None:
        case_ids = case_ids.where(RecoveryCase.id == interval.case_id)
    posting = func.coalesce(Invoice.posting_date, Invoice.issue_date)
    balance = func.greatest(Invoice.amount_total - Invoice.amount_paid, 0)
    eligible = Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF])
    overdue = eligible & (Invoice.due_date < interval.end_date) & (balance > 0)
    row = (await db.execute(select(
        func.count(), _sum(Invoice.amount_total), _sum(case((eligible, balance), else_=0)),
        _sum(case((overdue, balance), else_=0)), _count_if(overdue),
        _count_if(Invoice.status == InvoiceStatus.PAID),
        _count_if((Invoice.late_payment_probability >= Decimal("0.7")) & valid_score(contract)),
    ).where(*scoped, posting >= interval.start_date, posting <= interval.end_date))).one()
    counts = (await db.execute(select(func.count(), _count_if(RecoveryCase.status.in_(ACTIVE)))
        .where(RecoveryCase.id.in_(case_ids), *_event_window(interval, RecoveryCase.opened_at, start, stop)))).one()
    paid = await db.scalar(select(_sum(Payment.amount)).where(Payment.invoice_id.in_(invoice_ids),
        Payment.currency == interval.currency, Payment.status == PaymentStatus.SUCCESS,
        Payment.payment_date >= start, Payment.payment_date < stop))
    predictions = await db.scalar(select(func.count()).select_from(InferenceRecord).where(
        inference_scope(identity, interval.currency), InferenceRecord.model_kind == "b2b", InferenceRecord.status == "SUCCEEDED",
        InferenceRecord.completed_at >= start, InferenceRecord.completed_at < stop))
    diagnosis = [AgentDecision.recovery_case_id.in_(case_ids), AgentDecision.stage == AgentDecisionStage.DIAGNOSIS,
                 *_event_window(interval, AgentDecision.created_at, start, stop)]
    investigations = await db.scalar(select(func.count()).select_from(AgentDecision).where(*diagnosis))
    recommendations = await db.scalar(select(func.count()).select_from(AgentDecision).where(
        AgentDecision.recovery_case_id.in_(case_ids), AgentDecision.stage == AgentDecisionStage.INTERVENTION_RECOMMENDATION,
        *_event_window(interval, AgentDecision.created_at, start, stop)))
    recommendation = aliased(AgentDecision)
    completed = await db.scalar(select(func.count()).select_from(AgentDecision).where(*diagnosis,
        AgentDecision.output["investigation_id"].astext.is_not(None),
        select(recommendation.id).where(recommendation.recovery_case_id == AgentDecision.recovery_case_id,
            recommendation.stage == AgentDecisionStage.INTERVENTION_RECOMMENDATION,
            recommendation.output["investigation_id"].astext == AgentDecision.output["investigation_id"].astext).exists()))
    review_hours = await db.scalar(select(func.avg(func.extract("epoch", AuditLog.occurred_at - RecoveryAction.created_at) / 3600))
        .join(RecoveryAction, AuditLog.entity_id == RecoveryAction.id)
        .where(AuditLog.recovery_case_id.in_(case_ids), AuditLog.event_type.in_(["REVIEW_APPROVED", "REVIEW_REJECTED"]),
               *_event_window(interval, AuditLog.occurred_at, start, stop)))
    actions = (await db.execute(select(func.count(),
        _count_if((RecoveryAction.status != RecoveryActionStatus.PROPOSED) & RecoveryAction.recommended_action_type.is_not(None) & (RecoveryAction.recommended_action_type != RecoveryAction.action_type)),
        _count_if(RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL))
        .where(RecoveryAction.recovery_case_id.in_(case_ids), *_event_window(interval, RecoveryAction.created_at, start, stop)))).one()
    reviews = dict((await db.execute(select(AuditLog.event_type, func.count()).where(
        AuditLog.recovery_case_id.in_(case_ids), AuditLog.event_type.in_(["REVIEW_APPROVED", "REVIEW_REJECTED"]),
        *_event_window(interval, AuditLog.occurred_at, start, stop)).group_by(AuditLog.event_type))).all())
    return AnalyticsSummary(invoice_count=row[0], total_invoice_amount=row[1], outstanding_amount=row[2],
        overdue_amount=row[3], overdue_invoice_count=row[4], paid_invoice_count=row[5], high_risk_invoices=row[6],
        case_count=counts[0], active_cases=counts[1], recovered_amount=paid, predictions=predictions,
        investigations=investigations, completed_investigations=completed, recommendations=recommendations, average_review_hours=review_hours, actions=actions[0], policy_overrides=actions[1],
        pending_approvals=actions[2], approved_approvals=reviews.get("REVIEW_APPROVED", 0), rejected_approvals=reviews.get("REVIEW_REJECTED", 0))


async def get_date_analytics(db, interval: AnalyticsRange, identity: AnalyticsIdentity) -> AnalyticsOut:
    # Verify on every response, then reuse that contract only within this read.
    # No global cache: later requests still detect changed model artifacts.
    from app.ml.champion_serving import verified_contract
    contract = verified_contract()
    imported = await db.get(DatasetImport, "public_b2b_dataset")
    if imported and interval.source_timezone is None:
        scoped = (
            Invoice.company_id.in_(identity.company_ids),
            Invoice.currency == interval.currency,
        )
        imported_invoice = await db.scalar(
            select(Invoice.id)
            .where(*scoped, Invoice.invoice_number.like("B2B-%"))
            .limit(1)
        )
        other_invoice = await db.scalar(
            select(Invoice.id)
            .where(*scoped, ~Invoice.invoice_number.like("B2B-%"))
            .limit(1)
        )
        if imported_invoice is not None and other_invoice is None:
            interval = interval.model_copy(update={"source_timezone": "UTC"})
    stored = await stored_prediction_report(db, company_ids=identity.company_ids, currency=interval.currency,
        start_date=interval.start_date, end_date=interval.end_date, contract=contract)
    from app.api.recovery_cases import _to_list_item
    start, stop = interval.timestamps((interval.source_timezone or settings.business_timezone))
    scoped = [Invoice.company_id.in_(identity.company_ids), Invoice.currency == interval.currency]
    invoice_ids = select(Invoice.id).where(*scoped)
    workflow_invoices = invoice_ids
    if interval.workflow_date_axis == "invoice":
        workflow_invoices = invoice_ids.where(func.coalesce(Invoice.posting_date, Invoice.issue_date).between(interval.start_date, interval.end_date))
    case_ids = select(RecoveryCase.id).where(RecoveryCase.invoice_id.in_(workflow_invoices))
    if interval.case_id is not None:
        case_ids = case_ids.where(RecoveryCase.id == interval.case_id)
    posting = func.coalesce(Invoice.posting_date, Invoice.issue_date)
    cohort = [*scoped, posting >= interval.start_date, posting <= interval.end_date]
    case_cohort = [RecoveryCase.id.in_(case_ids), *_event_window(interval, RecoveryCase.opened_at, start, stop)]
    previous_end = interval.start_date - timedelta(days=1)
    previous_start = previous_end - (interval.end_date - interval.start_date)
    previous = interval.model_copy(update={"start_date": previous_start, "end_date": previous_end, "page": 1})
    summary = await _summary(db, interval, identity, contract=contract)
    previous_summary = await _summary(db, previous, identity, contract=contract)
    risk = case((~valid_score(contract), "UNSCORED"), (Invoice.payment_risk_model_version.is_(None), "UNSCORED"),
        (Invoice.payment_risk_scored_at.is_(None), "UNSCORED"), (Invoice.late_payment_probability.is_(None), "UNSCORED"),
        (Invoice.late_payment_probability < 0, "UNSCORED"), (Invoice.late_payment_probability > 1, "UNSCORED"),
        (Invoice.late_payment_probability < Decimal("0.3"), "LOW"),
        (Invoice.late_payment_probability < Decimal("0.7"), "MEDIUM"), else_="HIGH")
    risks = dict((await db.execute(select(risk, func.count()).where(*cohort).group_by(risk))).all())
    versions = stored['model_versions']
    # Ground truth exists only for fully reconciled stored payment histories.
    # Partial/seeded balances without complete payment events are not labels.
    cumulative = select(Payment.invoice_id.label("invoice_id"), Payment.payment_date.label("payment_date"),
        func.sum(Payment.amount).over(partition_by=Payment.invoice_id,
            order_by=(Payment.payment_date, Payment.id)).label("paid_to_date"))        .where(Payment.invoice_id.in_(invoice_ids), Payment.status == PaymentStatus.SUCCESS,
               Payment.currency == interval.currency, Payment.payment_date <= func.now()).subquery()
    payment_total = select(Payment.invoice_id.label("invoice_id"), func.sum(Payment.amount).label("total"))        .where(Payment.invoice_id.in_(invoice_ids), Payment.status == PaymentStatus.SUCCESS,
               Payment.currency == interval.currency, Payment.payment_date <= func.now()).group_by(Payment.invoice_id).subquery()
    settlement = select(cumulative.c.invoice_id, func.min(cumulative.c.payment_date).label("settled_at"))        .join(Invoice, Invoice.id == cumulative.c.invoice_id).where(cumulative.c.paid_to_date >= Invoice.amount_total)        .group_by(cumulative.c.invoice_id).subquery()
    due_adjustment = case((func.extract("isodow", Invoice.due_date) == 6, 2),
                          (func.extract("isodow", Invoice.due_date) == 7, 1), else_=0)
    adjusted_due = Invoice.due_date.op("+")(due_adjustment)
    late = cast(func.timezone((interval.source_timezone or settings.business_timezone), settlement.c.settled_at), Date) > adjusted_due
    resolved, late_count = (await db.execute(select(func.count(), _count_if(late)).select_from(InferenceRecord)
        .join(Invoice, Invoice.id == InferenceRecord.invoice_id)
        .join(settlement, settlement.c.invoice_id == Invoice.id).join(payment_total, payment_total.c.invoice_id == Invoice.id)
        .where(inference_scope(identity, interval.currency), InferenceRecord.model_kind == "b2b",
            InferenceRecord.status == "SUCCEEDED", InferenceRecord.completed_at >= start, InferenceRecord.completed_at < stop,
            InferenceRecord.probability.between(0, 1), InferenceRecord.model_version.is_not(None),
            Invoice.status == InvoiceStatus.PAID, Invoice.amount_paid >= Invoice.amount_total,
            payment_total.c.total == Invoice.amount_paid, settlement.c.settled_at >= InferenceRecord.completed_at))).one()
    outcomes = {"resolved_with_payment_events": resolved, "late": late_count, "on_time": resolved - late_count,
                "ground_truth_unavailable": summary.predictions - resolved}
    selected_action = case((RecoveryAction.status == RecoveryActionStatus.PROPOSED, "AWAITING_POLICY"), else_=cast(RecoveryAction.action_type, String))
    actions = dict((await db.execute(select(selected_action, func.count()).where(
        RecoveryAction.recovery_case_id.in_(case_ids), *_event_window(interval, RecoveryAction.created_at, start, stop))
        .group_by(selected_action))).all())
    statuses = dict((await db.execute(select(RecoveryCase.status, func.count()).where(*case_cohort).group_by(RecoveryCase.status))).all())
    # Month buckets preserve business-local boundaries for payment timestamps.
    invoice_month = _period(posting, interval.grain)
    invoice_series = {row[0]: (row[1], row[2]) for row in (await db.execute(select(invoice_month, func.count(), _sum(Invoice.amount_total))
        .where(*cohort).group_by(invoice_month))).all()}
    payment_month = _period(func.timezone((interval.source_timezone or settings.business_timezone), Payment.payment_date), interval.grain)
    payments = dict((await db.execute(select(payment_month, _sum(Payment.amount)).where(Payment.invoice_id.in_(invoice_ids),
        Payment.currency == interval.currency, Payment.status == PaymentStatus.SUCCESS,
        Payment.payment_date >= start, Payment.payment_date < stop).group_by(payment_month))).all())
    series = [AnalyticsBucket(month=month[:7] if interval.grain == "month" else month, invoice_count=invoice_series.get(month, (0, 0))[0],
        invoice_amount=invoice_series.get(month, (0, 0))[1], recovered_amount=payments.get(month, 0))
        for month in sorted(set(invoice_series) | set(payments))]
    # Table filters do not change the documented period KPI population.
    if interval.invoice_status is not None:
        if imported:
            cutoff = min(interval.end_date, imported.dataset_as_of)
            cutoff_stop = datetime.combine(cutoff + timedelta(days=1), datetime.min.time(), timezone.utc)
            observed_payments = select(Payment.invoice_id.label("invoice_id"), _sum(Payment.amount).label("paid")).where(
                Payment.status == PaymentStatus.SUCCESS, Payment.currency == interval.currency,
                Payment.payment_date < cutoff_stop, Payment.invoice_id.in_(invoice_ids)).group_by(Payment.invoice_id).subquery()
            observed_paid = func.coalesce(observed_payments.c.paid, 0)
            observed_balance = func.greatest(Invoice.amount_total - observed_paid, 0)
            observed_status = case((Invoice.status.in_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF]), cast(Invoice.status, String)),
                (observed_balance == 0, "PAID"),
                (Invoice.due_date < cutoff, "OVERDUE"), (observed_paid > 0, "PARTIALLY_PAID"), else_="SENT")
            matching_status = select(Invoice.id).outerjoin(observed_payments, observed_payments.c.invoice_id == Invoice.id).where(
                *scoped, case((Invoice.invoice_number.like("B2B-%"), observed_status), else_=cast(Invoice.status, String)) == interval.invoice_status.value)
            cohort.append(Invoice.id.in_(matching_status))
        else:
            cohort.append(Invoice.status == interval.invoice_status)
    if interval.case_status is not None:
        case_cohort.append(RecoveryCase.status == interval.case_status)
    if interval.risk is not None:
        cohort.append(risk == interval.risk)
        case_cohort.append(RecoveryCase.risk_level.is_(None) if interval.risk == "UNSCORED" else RecoveryCase.risk_level == interval.risk)
    # Search changes paginated tables, not aggregate population.
    if interval.search:
        pattern = "%" + interval.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matched = select(Company.id).where(Company.name.ilike(pattern, escape="\\"))
        match = Invoice.invoice_number.ilike(pattern, escape="\\") | Invoice.company_id.in_(matched) | (cast(Invoice.id, String).ilike(pattern, escape="\\"))
        cohort.append(match)
        case_cohort.append(RecoveryCase.invoice_id.in_(select(Invoice.id).where(*scoped, match)) |
                           cast(RecoveryCase.id, String).ilike(pattern, escape="\\"))
    invoice_total = await db.scalar(select(func.count()).select_from(Invoice).where(*cohort))
    case_total = await db.scalar(select(func.count()).select_from(RecoveryCase).where(*case_cohort))
    offset = (interval.page - 1) * interval.page_size
    invoice_sort = Invoice.due_date if interval.sort.startswith("due_") else Invoice.amount_total if interval.sort.startswith("amount_") else posting
    invoice_order = invoice_sort.asc() if interval.sort.endswith("asc") else invoice_sort.desc()
    case_order = RecoveryCase.opened_at.asc() if interval.sort.endswith("asc") else RecoveryCase.opened_at.desc()
    invoices = (await db.execute(select(Invoice).where(*cohort).options(selectinload(Invoice.company))
        .order_by(invoice_order, Invoice.id).offset(offset).limit(interval.page_size))).scalars().all()
    cases = (await db.execute(select(RecoveryCase).where(*case_cohort)
        .options(selectinload(RecoveryCase.invoice).selectinload(Invoice.company), selectinload(RecoveryCase.actions))
        .order_by(case_order, RecoveryCase.id).offset(offset).limit(interval.page_size))).scalars().all()
    invoice_case_ids = {str(invoice_id): str(case_id) for invoice_id, case_id in (await db.execute(
        select(RecoveryCase.invoice_id, RecoveryCase.id).where(RecoveryCase.invoice_id.in_([item.id for item in invoices])))).all()}
    tool_mode = AgentDecision.input_context["customer"]["read_only_investigation"]["mode"].astext
    tool_status = case((tool_mode == "constrained_deterministic_nl2sql", "completed"),
        (tool_mode == "unavailable", "unavailable"), else_="not_recorded")
    tool_counts = dict((await db.execute(select(tool_status, func.count()).where(
        AgentDecision.recovery_case_id.in_(case_ids), AgentDecision.stage == AgentDecisionStage.DIAGNOSIS,
        *_event_window(interval, AgentDecision.created_at, start, stop)).group_by(tool_status))).all())
    # SQL UNION paginates event history without loading every event into Python.
    from sqlalchemy import literal, union_all
    def event_select(model, kind, timestamp, status, action=None, recommendation=None, version=None, reason=None, tool=None, provenance=None, simulated=None):
        return select(cast(model.id, String).label("id"), cast(model.recovery_case_id, String).label("case_id"),
            timestamp.label("occurred_at"), literal(kind).label("kind"), cast(status, String).label("status"),
            (action if action is not None else literal(None)).label("action"),
            (recommendation if recommendation is not None else literal(None)).label("recommendation"),
            (version if version is not None else literal(None)).label("model_version"),
            (reason if reason is not None else literal(None)).label("reason"),
            (tool if tool is not None else literal(None)).label("tool_status"),
            (provenance if provenance is not None else cast(literal(None), JSONB)).label("provenance"),
            (simulated if simulated is not None else cast(literal(None), Boolean)).label("simulated"))
    inv_events = event_select(AgentDecision, "investigation", AgentDecision.created_at, AgentDecision.stage,
        version=AgentDecision.model_name, reason=AgentDecision.rationale, provenance=AgentDecision.output["provenance"],
        tool=case((AgentDecision.stage == AgentDecisionStage.DIAGNOSIS, tool_status), else_=None)).where(AgentDecision.recovery_case_id.in_(case_ids),
        *_event_window(interval, AgentDecision.created_at, start, stop))
    action_events = event_select(RecoveryAction, "action", RecoveryAction.created_at, RecoveryAction.status,
        simulated=cast(RecoveryAction.result["simulated"].astext, Boolean),
        action=cast(RecoveryAction.action_type, String), recommendation=cast(RecoveryAction.recommended_action_type, String),
        reason=select(PolicyDecision.reason).where(PolicyDecision.recovery_action_id == RecoveryAction.id)
            .order_by(PolicyDecision.evaluated_at.desc(), PolicyDecision.id).limit(1).scalar_subquery())
    action_events = action_events.where(RecoveryAction.recovery_case_id.in_(case_ids), *_event_window(interval, RecoveryAction.created_at, start, stop))
    approval_events = event_select(AuditLog, "approval", AuditLog.occurred_at, AuditLog.event_type, reason=AuditLog.description).where(
        AuditLog.recovery_case_id.in_(case_ids), AuditLog.event_type.in_(["REVIEW_APPROVED", "REVIEW_REJECTED", "REVIEW_REQUESTED"]),
        *_event_window(interval, AuditLog.occurred_at, start, stop))
    if interval.view == "approvals":
        reviewed = select(AuditLog.id).where(AuditLog.entity_id == RecoveryAction.id,
            AuditLog.event_type.in_(["REVIEW_APPROVED", "REVIEW_REJECTED"])).exists()
        history = union_all(approval_events, action_events.where(
            (RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL) | reviewed)).subquery()
    elif interval.view == "investigations":
        history = union_all(inv_events, action_events).subquery()
    else:
        history = union_all(inv_events, action_events, approval_events).subquery()
    history_query = select(history)
    if interval.approval_status:
        history_query = history_query.where(history.c.status == interval.approval_status)
    if interval.action:
        history_query = history_query.where(history.c.action == interval.action)
    if interval.policy_reason:
        pattern = "%" + interval.policy_reason.replace("%", "\\%").replace("_", "\\_") + "%"
        history_query = history_query.where(history.c.reason.ilike(pattern, escape="\\"))
    activity_total = await db.scalar(select(func.count()).select_from(history_query.subquery()))
    activities = [ActivityRecord(**row._mapping) for row in (await db.execute(history_query
        .order_by(history.c.occurred_at.desc(), history.c.id).offset(offset).limit(interval.page_size))).all()]
    inference_filters = [inference_scope(identity, interval.currency),
        InferenceRecord.started_at >= start, InferenceRecord.started_at < stop]
    inference_total = await db.scalar(select(func.count()).select_from(InferenceRecord).where(*inference_filters))
    inference_rows = []
    if interval.view == "ml":
        records = (await db.scalars(select(InferenceRecord).where(*inference_filters)
            .order_by(InferenceRecord.started_at.desc(), InferenceRecord.id).offset(offset).limit(interval.page_size))).all()
        inference_rows = [InferenceRecordOut(**{**{key:getattr(record,key) for key in InferenceRecordOut.model_fields},
            "id":str(record.id), "invoice_id":str(record.invoice_id) if record.invoice_id else None}) for record in records]
        activity_total = inference_total
    financial = await _financial_metrics(db, interval, identity, imported, contract=contract)

    reconciliation = await reconciliation_for_interval(
        db,
        interval,
        identity,
        cutoff=financial.cutoff,
    )

    historical_balances = {}
    if financial.reconstructable and invoices:
        cutoff_stop = datetime.combine(financial.cutoff + timedelta(days=1), datetime.min.time(), timezone.utc)
        paid_by_id = dict((await db.execute(select(Payment.invoice_id, _sum(Payment.amount)).where(
            Payment.invoice_id.in_([i.id for i in invoices]), Payment.status == PaymentStatus.SUCCESS,
            Payment.currency == interval.currency, Payment.payment_date < cutoff_stop).group_by(Payment.invoice_id))).all())
        for invoice in invoices:
            amount_paid = paid_by_id.get(invoice.id, Decimal(0))
            excluded = invoice.status in [InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF]
            balance = Decimal(0) if excluded else max(invoice.amount_total - amount_paid, Decimal(0))
            historical_balances[str(invoice.id)] = {"amount_paid": amount_paid, "outstanding": balance,
                "as_of": financial.cutoff, "status": invoice.status.value if excluded else "PAID" if balance == 0 else "OVERDUE" if invoice.due_date < financial.cutoff else "PARTIALLY_PAID" if amount_paid > 0 else "SENT"}

    historical = await historical_snapshot(db, interval, identity, (interval.source_timezone or settings.business_timezone))
    telemetry = await inference_telemetry(db, interval, identity, (interval.source_timezone or settings.business_timezone))
    return AnalyticsOut(reconciliation=reconciliation, dataset_as_of=imported.dataset_as_of if imported else None, grain=interval.grain,
        workflow_date_axis=interval.workflow_date_axis, financial=financial, metric_definitions=METRIC_DEFINITIONS,
        stored_predictions=stored['stored_predictions'], persisted_prediction_provenance=stored,
        historical=historical, historical_as_of_available=historical.available,
        inference_telemetry=telemetry, inference_records=inference_rows, inference_total=inference_total, inference_requests=telemetry["b2b"].requests,
        inference_errors=telemetry["b2b"].errors, inference_latency_ms=telemetry["b2b"].average_latency_ms,
        start_date=interval.start_date, end_date=interval.end_date, business_timezone=(interval.source_timezone or settings.business_timezone),
        currency=interval.currency, generated_at=datetime.now(timezone.utc), workspace_id=identity.workspace_id,
        summary=summary, changes_percent={key: ((Decimal(str(value)) - Decimal(str(previous_summary.model_dump()[key])))
            / abs(Decimal(str(previous_summary.model_dump()[key]))) * 100)
            if value is not None and previous_summary.model_dump()[key] not in (None, 0, Decimal(0)) else None
            for key, value in summary.model_dump().items()}, previous=previous_summary, previous_start_date=previous_start, previous_end_date=previous_end,
        series=series, risk_distribution=risks, action_distribution=actions, case_status_distribution=statuses,
        prediction_outcomes=outcomes, tool_status_distribution=tool_counts, model_versions=versions, invoice_case_ids=invoice_case_ids,
        historical_invoice_balances=historical_balances, invoices=[InvoiceOut.model_validate(item) for item in invoices], cases=[_to_list_item(item) for item in cases],
        activities=activities, page=interval.page, page_size=interval.page_size, invoice_total=invoice_total,
        case_total=case_total, activity_total=activity_total, semantics=SEMANTICS)
