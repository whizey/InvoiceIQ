"""As-of state uses recorded snapshots, never today's balances backdated.

Coverage starts at the additive migration baseline. Database triggers capture
all subsequent SQL/ORM mutations atomically with the business transaction.
"""
from datetime import datetime, timezone
from sqlalchemy import case, func, select, or_
from app.models import AnalyticsHistoryState, InvoiceStateSnapshot, CaseStateSnapshot, InferenceRecord
from app.schemas.analytics import HistoricalSnapshot, InferenceTelemetry


async def historical_snapshot(db, interval, identity, business_timezone):
    start, stop = interval.timestamps(business_timezone)
    effective = min(stop, datetime.now(timezone.utc))
    coverage = await db.scalar(select(AnalyticsHistoryState.started_at).where(AnalyticsHistoryState.name == "financial_snapshots"))
    if coverage is None or effective <= coverage:
        return HistoricalSnapshot(effective_as_of=effective, coverage_started_at=coverage,
            reason="Selected cutoff precedes recorded history. Earlier balances/statuses cannot be reconstructed from current state.")
    # PostgreSQL DISTINCT ON selects one state at the exclusive cutoff for each entity.
    invoices = select(InvoiceStateSnapshot).where(InvoiceStateSnapshot.company_id.in_(identity.company_ids),
        InvoiceStateSnapshot.observed_at < effective).distinct(InvoiceStateSnapshot.invoice_id).order_by(
            InvoiceStateSnapshot.invoice_id, InvoiceStateSnapshot.observed_at.desc(), InvoiceStateSnapshot.id.desc()).subquery()
    eligible = invoices.c.status.notin_(["CANCELLED", "WRITTEN_OFF"])
    balance = func.greatest(invoices.c.amount_total - invoices.c.amount_paid, 0)
    overdue = eligible & (invoices.c.due_date < interval.end_date) & (balance > 0)
    row = (await db.execute(select(func.count(),
        func.coalesce(func.sum(case((eligible, balance), else_=0)), 0),
        func.coalesce(func.sum(case((overdue, balance), else_=0)), 0),
        func.coalesce(func.sum(case((overdue, 1), else_=0)), 0)).where(
            invoices.c.currency == interval.currency,
            invoices.c.posting_date >= interval.start_date, invoices.c.posting_date <= interval.end_date))).one()
    cases = select(CaseStateSnapshot).where(CaseStateSnapshot.company_id.in_(identity.company_ids),
        CaseStateSnapshot.observed_at < effective).distinct(CaseStateSnapshot.case_id).order_by(
            CaseStateSnapshot.case_id, CaseStateSnapshot.observed_at.desc(), CaseStateSnapshot.id.desc()).subquery()
    statuses = dict((await db.execute(select(cases.c.status, func.count()).where(
        cases.c.invoice_id.in_(select(invoices.c.invoice_id).where(invoices.c.currency == interval.currency)),
        cases.c.opened_at >= start, cases.c.opened_at < stop).group_by(cases.c.status))).all())
    return HistoricalSnapshot(available=True, coverage_started_at=coverage, effective_as_of=effective,
        invoice_count=row[0], outstanding_amount=row[1], overdue_amount=row[2], overdue_invoice_count=row[3],
        active_cases=sum(statuses.get(s,0) for s in ("OPEN","MONITORING","ESCALATED")), case_status_distribution=statuses,
        reason="Latest observed state strictly before the selected exclusive cutoff (or now for future dates). Invoice posting cohort and case opening cohort use their recorded values. History begins at the observed migration baseline; backdated corrections become visible when recorded.")


def inference_scope(identity, currency):
    return ((InferenceRecord.currency == currency) | InferenceRecord.currency.is_(None)) & or_(InferenceRecord.company_id.in_(identity.company_ids),
               (InferenceRecord.company_id.is_(None)) & (InferenceRecord.workspace_id == identity.workspace_id))


async def inference_telemetry(db, interval, identity, business_timezone):
    start, stop = interval.timestamps(business_timezone)
    coverage = await db.scalar(select(AnalyticsHistoryState.started_at).where(AnalyticsHistoryState.name == "inference_records"))
    result = {}
    for kind in ("b2b", "bpi"):
        filters = [inference_scope(identity, interval.currency), InferenceRecord.model_kind == kind,
                   InferenceRecord.source != "legacy_snapshot", InferenceRecord.started_at >= start,
                   InferenceRecord.started_at < stop]
        count, success, errors, average, p95 = (await db.execute(select(func.count(),
            func.coalesce(func.sum(case((InferenceRecord.status == "SUCCEEDED",1),else_=0)),0),
            func.coalesce(func.sum(case((InferenceRecord.status.in_(["FAILED", "CANCELLED"]),1),else_=0)),0),
            func.avg(InferenceRecord.latency_ms),
            func.percentile_cont(0.95).within_group(InferenceRecord.latency_ms)).where(*filters))).one()
        versions = dict((await db.execute(select(InferenceRecord.model_version,func.count()).where(
            *filters, InferenceRecord.status == "SUCCEEDED", InferenceRecord.model_version.is_not(None))
            .group_by(InferenceRecord.model_version))).all())
        result[kind] = InferenceTelemetry(requests=count, succeeded=success, errors=errors, in_flight=count-success-errors,
            average_latency_ms=average, p95_latency_ms=p95, coverage_started_at=coverage,
            complete_interval=coverage is not None and start >= coverage, model_versions=versions)
    return result
