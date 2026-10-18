"""Adapter for the original six-card dashboard.

Same as-of rules as analytics_history.historical_snapshot and the source branch
of date_analytics._financial_metrics. Never substitute present balances for an
unobserved historical state. Financial services and models remain unchanged.
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import Date, DateTime, String, case, cast, column, false, func, literal, select, true, values
from app.core.config import settings
from app.models import (
    AnalyticsHistoryState,
    Company,
    DatasetImport,
    Invoice,
    InvoiceStateSnapshot,
    Payment,
    RecoveryCase,
)
from app.models.enums import PaymentStatus
from app.schemas.preserved_dashboard import Band, ChartPoint, InvoiceRow, PreservedDashboard, RetainedMetrics
from app.services.persisted_predictions import valid_score


def summed(value):
    return func.coalesce(func.sum(value), 0)


def counted(condition):
    return summed(case((condition, 1), else_=0))


def cutoff(day, zone):
    return datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(zone))


def scope(identity, q):
    filters = [Invoice.company_id.in_(identity.company_ids), Invoice.currency == q.currency]
    if q.company_id:
        filters.append(Invoice.company_id == q.company_id)
    if q.invoice_number:
        key = "%" + q.invoice_number.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters.append(Invoice.invoice_number.ilike(key, escape="\\"))
    if q.company_search:
        key = "%" + q.company_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters.append(Invoice.company_id.in_(
            select(Company.id).where(Company.name.ilike(key, escape="\\"))
        ))
    if q.search:
        value = "%" + q.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters.append(
            Invoice.invoice_number.ilike(value, escape="\\")
            | Invoice.company_id.in_(select(Company.id).where(Company.name.ilike(value, escape="\\")))
            | Invoice.id.in_(
                select(RecoveryCase.invoice_id).where(cast(RecoveryCase.id, String).ilike(value, escape="\\"))
            )
        )
    return filters


async def state_at(db, q, identity, as_of, zone, imported):
    """One state per invoice, before the exclusive cutoff; no fanout."""
    scoped = scope(identity, q)
    stop = min(cutoff(as_of, zone), datetime.now(timezone.utc))
    if imported:
        # Only the existing reconciled import has a complete original payment ledger.
        foreign = await db.scalar(select(func.count()).where(*scoped, ~Invoice.invoice_number.like("B2B-%")))
        if not foreign:
            paid = (
                select(Payment.invoice_id, summed(Payment.amount).label("paid"))
                .where(
                    Payment.invoice_id.in_(select(Invoice.id).where(*scoped)),
                    Payment.currency == q.currency,
                    Payment.status == PaymentStatus.SUCCESS,
                    Payment.payment_date < cutoff(min(as_of, imported.dataset_as_of), zone),
                )
                .group_by(Payment.invoice_id)
                .subquery()
            )
            return select(
                Invoice.id.label("invoice_id"),
                Invoice.company_id,
                Invoice.amount_total,
                func.coalesce(paid.c.paid, 0).label("amount_paid"),
                Invoice.currency,
                Invoice.posting_date,
                Invoice.due_date,
                cast(Invoice.status, String).label("status"),
            ).outerjoin(paid, paid.c.invoice_id == Invoice.id).where(
                *scoped, Invoice.posting_date <= min(as_of, imported.dataset_as_of)
            ).subquery(), "Original reconciled source payments through cutoff; later payments excluded."
    # Current balances are observable; past balances require recorded history.
    if imported is None and as_of == datetime.now(ZoneInfo(zone)).date():
        state = (
            select(
                Invoice.id.label("invoice_id"), Invoice.company_id,
                Invoice.amount_total, Invoice.amount_paid, Invoice.currency,
                Invoice.posting_date, Invoice.due_date,
                cast(Invoice.status, String).label("status"),
            )
            .where(*scoped, Invoice.posting_date <= as_of)
            .subquery("live_business_day_invoice_state")
        )
        return state, "Current observed business-day balance; not a retrospective snapshot."

    coverage = await db.scalar(
        select(AnalyticsHistoryState.started_at).where(AnalyticsHistoryState.name == "financial_snapshots")
    )
    if coverage is None or stop <= coverage:
        return None, "Selected cutoff precedes recorded state history; historical balances are unavailable."
    state = (
        select(InvoiceStateSnapshot)
        .where(
            InvoiceStateSnapshot.company_id.in_(identity.company_ids),
            InvoiceStateSnapshot.observed_at < stop,
        )
        .distinct(InvoiceStateSnapshot.invoice_id)
        .order_by(
            InvoiceStateSnapshot.invoice_id,
            InvoiceStateSnapshot.observed_at.desc(),
            InvoiceStateSnapshot.id.desc(),
        )
        .cte("selected_invoice_state")
        .prefix_with("MATERIALIZED")
    )
    return (
        state,
        "Latest database-maintained invoice state before cutoff; historical coverage is not backdated.",
    )


def cohort(q, state, as_of, zone):
    c = state.c
    balance = case(
        (c.status.notin_(["CANCELLED", "WRITTEN_OFF"]), func.greatest(c.amount_total - c.amount_paid, 0)),
        else_=0,
    )
    age = cast(as_of, Date) - c.due_date
    try:
        trusted = valid_score()
    except RuntimeError:
        trusted = false()
    probability = case((trusted, Invoice.late_payment_probability), else_=None)
    # No accounting holiday calendar exists. Match existing historical service's
    # contractual overdue rule, explicitly disclose the absent policy adjustment.
    filters = [c.currency == q.currency, c.posting_date <= as_of]
    axis = c.due_date if q.date_basis == "due" else c.posting_date
    if q.date_basis == "payment":
        payments = select(Payment.invoice_id).where(
            Payment.status == PaymentStatus.SUCCESS,
            Payment.currency == q.currency,
            Payment.payment_date < cutoff(as_of, zone),
        )
        if q.start_date:
            payments = payments.where(
                Payment.payment_date >= datetime.combine(q.start_date, time.min, ZoneInfo(zone))
            )
        if q.end_date:
            payments = payments.where(Payment.payment_date < cutoff(q.end_date, zone))
        filters.append(c.invoice_id.in_(payments))
    else:
        if q.start_date:
            filters.append(axis >= q.start_date)
        if q.end_date:
            filters.append(axis <= q.end_date)
    statuses = {
        "outstanding": balance > 0,
        "open": (balance > 0) & (c.amount_paid <= 0),
        "partial": (balance > 0) & (c.amount_paid > 0),
        "overdue": (balance > 0) & (age > 0),
        "settled": (c.amount_total > 0)
        & (c.amount_paid >= c.amount_total)
        & c.status.notin_(["CANCELLED", "WRITTEN_OFF"]),
        "cancelled": c.status == "CANCELLED",
        "written_off": c.status == "WRITTEN_OFF",
    }
    for key, value in [
        ('posted_from', c.posting_date >= q.posted_from if q.posted_from else None),
        ('posted_to', c.posting_date <= q.posted_to if q.posted_to else None),
        ('due_from', c.due_date >= q.due_from if q.due_from else None),
        ('due_to', c.due_date <= q.due_to if q.due_to else None),
        ('amount_min', c.amount_total >= q.amount_min if q.amount_min is not None else None),
        ('amount_max', c.amount_total <= q.amount_max if q.amount_max is not None else None),
        ('paid_min', c.amount_paid >= q.paid_min if q.paid_min is not None else None),
        ('paid_max', c.amount_paid <= q.paid_max if q.paid_max is not None else None),
        ('balance_min', balance >= q.balance_min if q.balance_min is not None else None),
        ('balance_max', balance <= q.balance_max if q.balance_max is not None else None),
        ('days_min', func.greatest(age, 0) >= q.days_min if q.days_min is not None else None),
        ('days_max', func.greatest(age, 0) <= q.days_max if q.days_max is not None else None),
        ('risk_min', probability >= q.risk_min / 100 if q.risk_min is not None else None),
        ('risk_max', probability <= q.risk_max / 100 if q.risk_max is not None else None),
    ]:
        if value is not None:
            filters.append(value)
    if q.case_presence == 'yes':
        filters.append(c.invoice_id.in_(select(RecoveryCase.invoice_id)))
    elif q.case_presence == 'no':
        filters.append(~c.invoice_id.in_(select(RecoveryCase.invoice_id)))
    if q.status != "all":
        filters.append(statuses[q.status])
    if q.risk == "unscored":
        filters.append(~trusted)
    elif q.risk == "low":
        filters.extend([trusted, probability < Decimal(".3")])
    elif q.risk == "medium":
        filters.extend([trusted, probability >= Decimal(".3"), probability < Decimal(".7")])
    elif q.risk == "high":
        filters.extend([trusted, probability >= Decimal(".7")])
    return balance, age, probability, filters


async def month_end_balances(db, q, identity, series, as_of, zone, imported):
    """One bounded SQL query for covered state cutoffs; no per-invoice Python loads."""
    if imported or not series:
        return
    coverage = await db.scalar(
        select(AnalyticsHistoryState.started_at).where(AnalyticsHistoryState.name == "financial_snapshots")
    )
    if coverage is None:
        return
    moments = []
    for period in series:
        if q.grain == "day":
            next_period = period + timedelta(days=1)
        elif q.grain == "week":
            next_period = period + timedelta(days=7)
        else:
            months = {"month": 1, "quarter": 3, "year": 12}[q.grain]
            ordinal = period.year * 12 + period.month - 1 + months
            year, month = divmod(ordinal, 12)
            next_period = period.replace(year=year, month=month + 1, day=1)
        stop = min(cutoff(min(next_period - timedelta(days=1), as_of), zone), datetime.now(timezone.utc))
        if stop > coverage:
            moments.append((period, stop))
    if not moments:
        return
    periods = (
        values(column("period", Date), column("stop", DateTime(timezone=True)), name="cutoffs")
        .data(moments)
        .alias("cutoffs")
    )
    history = (
        select(InvoiceStateSnapshot)
        .where(
            InvoiceStateSnapshot.company_id.in_(identity.company_ids),
            InvoiceStateSnapshot.observed_at < periods.c.stop,
        )
        .distinct(InvoiceStateSnapshot.invoice_id)
        .order_by(
            InvoiceStateSnapshot.invoice_id,
            InvoiceStateSnapshot.observed_at.desc(),
            InvoiceStateSnapshot.id.desc(),
        )
        .correlate(periods)
        .lateral("historical_state")
    )
    b, _, _, predicates = cohort(q, history, as_of, zone)
    statement = (
        select(periods.c.period, summed(b))
        .select_from(periods)
        .join(history, true())
        .join(Invoice, Invoice.id == history.c.invoice_id)
        .where(*predicates, *scope(identity, q))
        .group_by(periods.c.period)
    )
    balances = dict((await db.execute(statement)).all())
    for period, _ in moments:
        series[period].open = balances.get(period, Decimal(0))


def payment_filters(q, identity, as_of, zone):
    clauses = [
        Payment.invoice_id.in_(select(Invoice.id).where(*scope(identity, q))),
        Payment.currency == q.currency,
        Payment.status == PaymentStatus.SUCCESS,
        Payment.payment_date < cutoff(as_of, zone),
    ]
    if q.start_date:
        clauses.append(Payment.payment_date >= datetime.combine(q.start_date, time.min, ZoneInfo(zone)))
    if q.end_date:
        clauses.append(Payment.payment_date < cutoff(q.end_date, zone))
    return clauses


async def build_dashboard(db, q, identity, *, export=False):
    imported = await db.get(DatasetImport, "public_b2b_dataset")
    if imported is not None:
        selected_scope = scope(identity, q)
        imported_id = await db.scalar(
            select(Invoice.id).where(
                *selected_scope, Invoice.invoice_number.like("B2B-%")
            ).limit(1)
        )
        nonimported_id = await db.scalar(
            select(Invoice.id).where(
                *selected_scope, ~Invoice.invoice_number.like("B2B-%")
            ).limit(1)
        )
        if imported_id is None or nonimported_id is not None:
            imported = None

    zone = "UTC" if imported else settings.business_timezone
    today = imported.dataset_as_of if imported else datetime.now(ZoneInfo(zone)).date()
    as_of = q.as_of or today
    if as_of > today:
        raise HTTPException(422, "As-of cannot exceed the latest available business date")
    state, reason = await state_at(db, q, identity, as_of, zone, imported)
    available = state is not None
    if state is None:
        # Face amounts and invoice metadata remain readable; balances stay null.
        state = (
            select(
                Invoice.id.label("invoice_id"),
                Invoice.company_id,
                Invoice.amount_total,
                Invoice.amount_paid,
                Invoice.currency,
                Invoice.posting_date,
                Invoice.due_date,
                cast(Invoice.status, String).label("status"),
            )
            .where(*scope(identity, q))
            .subquery()
        )
    c = state.c
    balance, age, probability, filters = cohort(q, state, as_of, zone)
    state_filters = (q.status != 'all' or q.risk != 'any' or any(
        getattr(q, key) is not None for key in
        ('paid_min','paid_max','balance_min','balance_max','days_min','days_max','risk_min','risk_max')
    ))
    if not available and state_filters:
        raise HTTPException(422, "Historical filters require state coverage at the selected cutoff")
    base = (
        select()
        .select_from(state)
        .join(Invoice, Invoice.id == c.invoice_id)
        .where(*filters, *scope(identity, q))
    )
    overdue = (balance > 0) & (age > 0)
    row = (
        (
            await db.execute(
                base.with_only_columns(
                    func.count().label("invoice_count"),
                    summed(c.amount_total).label("billed"),
                    func.count(func.distinct(c.company_id)).label("company_count"),
                    summed(case((c.status != "CANCELLED", c.amount_total), else_=0)).label("invoiced"),
                    summed(balance).label("open"),
                    counted(balance > 0).label("open_count"),
                    summed(case((overdue, balance), else_=0)).label("overdue"),
                    counted(overdue).label("overdue_count"),
                    counted((balance > 0) & probability.is_not(None)).label("scored_open"),
                    counted((balance > 0) & (c.amount_paid > 0)).label("partial_count"),
                    counted(
                        (c.amount_total > 0)
                        & (c.amount_paid >= c.amount_total)
                        & c.status.notin_(["CANCELLED", "WRITTEN_OFF"])
                    ).label("settled_count"),
                    counted(c.status == "CANCELLED").label("cancelled_count"),
                    counted(c.status == "WRITTEN_OFF").label("written_off_count"),
                    func.percentile_cont(0.5).within_group(c.amount_total).label("median_invoice"),
                )
            )
        )
        .mappings()
        .one()
    )
    values = dict(row)
    payfilters = payment_filters(q, identity, as_of, zone)
    if (q.status != 'all' or q.risk != 'any' or q.invoice_number or q.company_search
        or q.posted_from or q.posted_to or q.due_from or q.due_to
        or q.case_presence != 'any' or any(getattr(q,k) is not None for k in
        ('amount_min','amount_max','paid_min','paid_max','balance_min','balance_max',
         'days_min','days_max','risk_min','risk_max'))):
        _, _, _, attribute_filters = cohort(
            q.model_copy(update={"start_date": None, "end_date": None, "date_basis": "posting"}),
            state,
            as_of,
            zone,
        )
        payment_invoice_ids = (
            select(c.invoice_id)
            .select_from(state)
            .join(Invoice, Invoice.id == c.invoice_id)
            .where(*attribute_filters, *scope(identity, q))
        )
        payfilters.append(Payment.invoice_id.in_(payment_invoice_ids))
    values["collected"] = await db.scalar(select(summed(Payment.amount)).where(*payfilters))
    values["overdue_share"] = values["overdue"] / values["open"] * 100 if values["open"] else None
    if not available:
        for key in [
            "invoiced",
            "billed",
            "open",
            "open_count",
            "overdue",
            "overdue_count",
            "overdue_share",
            "scored_open",
            "partial_count",
            "settled_count",
            "cancelled_count",
            "written_off_count",
        ]:
            values[key] = None
    metric = RetainedMetrics(**values)
    if not available:
        # Count is a current metadata cohort, never labeled historical state.
        reason += " Invoice metadata counts are current; cancelled eligibility cannot be backdated."
    aging = []
    risk = []
    due = []
    top = []
    if available:
        ageband = case(
            (age <= 0, "not_due"),
            (age <= 30, "1-30"),
            (age <= 60, "31-60"),
            (age <= 90, "61-90"),
            else_="90+",
        )
        aging = [
            Band(key=k, count=n, amount=a)
            for k, n, a in (
                await db.execute(
                    base.with_only_columns(ageband, func.count(), summed(balance))
                    .where(balance > 0)
                    .group_by(ageband)
                )
            ).all()
        ]
        bin_ = case(
            (probability.is_(None), "Not scored"),
            (probability < Decimal(".2"), "0–20%"),
            (probability < Decimal(".4"), "20–40"),
            (probability < Decimal(".6"), "40–60"),
            (probability < Decimal(".8"), "60–80"),
            else_="80–100",
        )
        risk = [
            Band(key=k, count=n, amount=a)
            for k, n, a in (
                await db.execute(
                    base.with_only_columns(bin_, func.count(), summed(balance))
                    .where(balance > 0)
                    .group_by(bin_)
                )
            ).all()
        ]
        week = case(
            (age > 0, "Past due"), else_=func.to_char(func.date_trunc("week", c.due_date), "YYYY-MM-DD")
        )
        due = [
            Band(key=k, count=n, amount=a)
            for k, n, a in (
                await db.execute(
                    base.with_only_columns(week, func.count(), summed(balance))
                    .where(balance > 0, c.due_date < as_of + timedelta(days=56))
                    .group_by(week)
                    .order_by(week)
                )
            ).all()
        ]
        company_query = base.join(Company, Company.id == c.company_id).with_only_columns(
            c.company_id.label("id"),
            Company.name.label("name"),
            counted(balance > 0).label("count"),
            summed(balance).label("open"),
            summed(case((overdue, balance), else_=0)).label("overdue"),
            func.max(case((balance > 0, func.greatest(age, 0)), else_=0)).label("oldest"),
            func.avg(case((balance > 0, probability), else_=None)).label("risk"),
        )
        top = [
            dict(r)
            for r in (
                await db.execute(
                    company_query.group_by(c.company_id, Company.name)
                    .having(summed(balance) > 0)
                    .order_by(summed(case((overdue, balance), else_=0)).desc(), c.company_id)
                    .limit(7)
                )
            ).mappings()
        ]
    # Keep monthly semantics even if another date axis is selected for the table.
    period = cast(func.date_trunc(q.grain, c.posting_date), Date)
    series = {}
    if available:
        for p, a in (
            await db.execute(
                base.with_only_columns(
                    period, summed(case((c.status != "CANCELLED", c.amount_total), else_=0))
                )
                .group_by(period)
                .order_by(period)
                .limit(401)
            )
        ).all():
            series[p] = ChartPoint(period=p, invoiced=a, collected=0)
    pperiod = cast(func.date_trunc(q.grain, func.timezone(zone, Payment.payment_date)), Date)
    for p, a in (
        await db.execute(
            select(pperiod, summed(Payment.amount))
            .where(*payfilters)
            .group_by(pperiod)
            .order_by(pperiod)
            .limit(401)
        )
    ).all():
        entry = series.setdefault(p, ChartPoint(period=p, invoiced=0, collected=0))
        entry.collected = a
    if len(series) > 400:
        raise HTTPException(
            422,
            "More than 400 chart buckets. Choose a coarser existing Group by; results were not truncated.",
        )
    await month_end_balances(db, q, identity, series, as_of, zone, imported)

    # The current chart point uses the same scoped live balance as the KPI.
    # Earlier periods retain their recorded historical values or null.
    if imported is None and as_of == datetime.now(ZoneInfo(zone)).date() and available:
        if q.grain == "day":
            current_period = as_of
        elif q.grain == "week":
            current_period = as_of - timedelta(days=as_of.weekday())
        elif q.grain == "month":
            current_period = as_of.replace(day=1)
        elif q.grain == "quarter":
            current_period = as_of.replace(
                month=((as_of.month - 1) // 3) * 3 + 1, day=1
            )
        elif q.grain == "year":
            current_period = as_of.replace(month=1, day=1)
        else:
            raise AssertionError("Unsupported chart grain")

        if current_period in series:
            series[current_period].open = values["open"]

    daily_start = as_of - timedelta(days=181)
    pday = cast(func.timezone(zone, Payment.payment_date), Date)
    daily = [
        {"date": d, "amount": a}
        for d, a in (
            await db.execute(
                select(pday, summed(Payment.amount))
                .where(*payfilters, pday >= daily_start)
                .group_by(pday)
                .order_by(pday)
            )
        ).all()
    ]
    case_id = (
        select(RecoveryCase.id)
        .where(RecoveryCase.invoice_id == c.invoice_id)
        .correlate(state)
        .limit(1)
        .scalar_subquery()
    )
    rows_query = base.join(Company, Company.id == c.company_id).with_only_columns(
        c.invoice_id.label("id"),
        Invoice.invoice_number,
        c.company_id,
        Company.name.label("company"),
        c.posting_date,
        c.due_date,
        c.amount_total,
        (c.amount_paid if available else literal(None)).label("amount_paid"),
        (balance if available else literal(None)).label("balance"),
        (func.greatest(age, 0) if available else literal(None)).label("days_past_due"),
        c.status,
        probability.label("probability"),
        Invoice.payment_term,
        case_id.label("case_id"),
    )
    sort = {
        "dpd_desc": c.due_date.asc(), "dpd_asc": c.due_date.desc(),
        "amount_desc": c.amount_total.desc(), "amount_asc": c.amount_total.asc(),
        "posting_desc": c.posting_date.desc(), "posting_asc": c.posting_date.asc(),
        "invoice_asc": Invoice.invoice_number.asc(), "invoice_desc": Invoice.invoice_number.desc(),
        "company_asc": Company.name.asc(), "company_desc": Company.name.desc(),
        "due_asc": c.due_date.asc(), "due_desc": c.due_date.desc(),
        "paid_asc": c.amount_paid.asc(), "paid_desc": c.amount_paid.desc(),
        "balance_asc": balance.asc(), "balance_desc": balance.desc(),
        "risk_asc": probability.asc().nulls_last(), "risk_desc": probability.desc().nulls_last(),
    }[q.sort]
    total = metric.invoice_count
    if export and total > 10000:
        raise HTTPException(
            422,
            "Export exceeds 10,000 invoices. Narrow the existing filters; no partial export was generated.",
        )
    rows = [
        InvoiceRow(**r)
        for r in (
            await db.execute(
                rows_query.order_by(sort, c.invoice_id)
                .offset(0 if export else (q.page - 1) * q.page_size)
                .limit(10000 if export else q.page_size)
            )
        ).mappings()
    ]
    companies = [
        {"id": str(cid), "name": name}
        for cid, name in (
            await db.execute(
                select(Company.id, Company.name)
                .where(Company.id.in_(identity.company_ids))
                .order_by(Company.name)
                .limit(1000)
            )
        ).all()
    ]
    comparison = None
    if q.compare_date:
        if q.compare_date >= as_of:
            raise HTTPException(422, "Comparison cutoff must precede As of")
        prior = await build_dashboard(
            db, q.model_copy(update={"as_of": q.compare_date, "compare_date": None}), identity
        )
        comparison = prior.metrics if prior.historical_available else None
    return PreservedDashboard(
        generated_at=datetime.now(timezone.utc),
        as_of=as_of,
        start_date=q.start_date,
        end_date=q.end_date,
        timezone=zone,
        workspace=identity.workspace_id,
        currency=q.currency,
        currency_verified=not bool(imported),
        currency_note="Import assigned INR, but source CSV has no currency column; denomination is unverified. No conversion performed."
        if imported
        else "Stored currency metadata; no conversion performed.",
        historical_available=available,
        historical_reason=reason,
        metrics=metric,
        comparison=comparison,
        series=sorted(series.values(), key=lambda x: x.period),
        aging=aging,
        risk=risk,
        daily=daily,
        due=due,
        companies=companies,
        top=top,
        invoices=rows,
        total=total,
        page=q.page,
        page_size=q.page_size,
        chart_notes={
            "open_line": "Line uses covered end-of-bucket state snapshots (current partial bucket through As of); gaps have no recorded history. Source imports without state coverage do not receive an invented line.",
            "bridge": "No complete dated cancellation/write-off ledger to reconcile the Q3 waterfall.",
            "pipeline": "Original New / Investigating / Action taken stages have no authoritative mapping to stored case status; not inferred.",
            "calendar": "No configured accounting holiday/grace calendar exists. Existing backend uses contractual due dates for aging, separately from weekend-adjusted ML labels.",
            "settlement": "Settled-late histogram requires reconciled full-settlement events and the original grace-day policy; no assumed labels.",
            "allocation": "No dated write-off amounts or credit ledger to reconcile the billed allocation; face amounts are not substituted for losses.",
            "limits": "Time series capped at 400 buckets; select a coarser existing Group by for longer periods. Heatmap covers the last 182 days through As of.",
        },
    )
