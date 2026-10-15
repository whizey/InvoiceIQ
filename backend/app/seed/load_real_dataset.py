"""Import the public corpus into a dedicated database; never touch development data."""
from __future__ import annotations

import asyncio
import argparse
import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ml.champion_serving import champion_predict_many, verified_contract
from app.models import Company, Invoice, Payment, PaymentEvent, DatasetImport
from app.services.payment_risk_scoring import apply_prediction
from app.seed.offline_metrics import import_bpi_evaluations
from app.models.enums import CompanySegment, InvoiceStatus, PaymentEventType, PaymentMethod, PaymentStatus
from app.services.payment_risk_context import PriorInvoiceSnapshot, compute_payment_risk_features

DATASET = Path(__file__).resolve().parents[3] / 'data/raw/b2b_primary/b2b_invoice_payment_45839.csv'
NAMESPACE = UUID('5265e212-06bf-42da-9960-f95549149034')


def identity(kind: str, value: object) -> UUID:
    return uuid5(NAMESPACE, f'{kind}:{value}')


def resolve_dataset_path(path: Path | None = None) -> Path:
    candidate = path or Path(os.environ.get("REAL_DATASET_PATH", str(DATASET)))
    candidate = candidate.expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Public B2B CSV not found: {candidate}. Set REAL_DATASET_PATH to an existing local CSV."
        )
    return candidate


def read_dataset(path: Path | None = None) -> pd.DataFrame:
    # Decimal from the source text, never from a binary floating-point aggregate.
    frame = pd.read_csv(resolve_dataset_path(path), converters={"Amount": Decimal})
    required = {"Cust_Num", "Document_No", "Amount", "Doc_Date", "Posting_Date",
                "Net_Due_Date", "Clearing_date", "Payment_Term", "Customer_Name",
                "Payment_Method_description"}
    if not required.issubset(frame.columns):
        raise ValueError("Public corpus is missing required source columns")
    for column in ('Doc_Date', 'Posting_Date', 'Net_Due_Date', 'Clearing_date'):
        frame[column] = pd.to_datetime(frame[column], format='%m/%d/%Y', errors='raise')
        if frame[column].isna().any():
            raise ValueError(f"Missing source dates: {column}")
    if len(frame) != 45839 or frame.Cust_Num.nunique() != 69 or frame.Document_No.duplicated().any():
        raise ValueError('Public corpus identity/row-count validation failed')
    if frame.Cust_Num.isna().any() or frame.Document_No.isna().any():
        raise ValueError('Missing source identity')
    if any(not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01"))
           for amount in frame.Amount):
        raise ValueError('Invalid financial amounts')
    return frame.sort_values(['Posting_Date', 'Document_No']).reset_index(drop=True)


def dataset_metadata(frame: pd.DataFrame) -> dict:
    # Last observed clearing/posting is the extraction cutoff. Future due dates
    # are contractual dates, not evidence that the dataset was observed then.
    as_of = max(frame.Posting_Date.max(), frame.Clearing_date.max()).date()
    return {'dataset_as_of': as_of.isoformat(), 'row_count': len(frame),
            'customer_count': int(frame.Cust_Num.nunique()),
            'amount_total': str(sum(frame.Amount, Decimal("0"))),
            'outstanding_at_as_of': int((frame.Clearing_date.dt.date > as_of).sum())}


def posting_features(frame: pd.DataFrame, *, existing_ids: set[UUID] | None = None):
    """Use the runtime's leakage-safe posting-time feature builder unchanged."""
    histories: dict[int, list[PriorInvoiceSnapshot]] = {}
    for row in frame.itertuples(index=False):
        history = histories.setdefault(int(row.Cust_Num), [])
        features = (None if existing_ids is not None and identity('invoice', row.Document_No) in existing_ids else
            compute_payment_risk_features(amount=float(row.Amount),
                posting_date=row.Posting_Date.date(), payment_term=int(row.Payment_Term), history=history))
        yield row, features
        history.append(PriorInvoiceSnapshot(str(row.Document_No), float(row.Amount),
            row.Posting_Date.date(), row.Net_Due_Date.date(), row.Clearing_date.date()))


async def reconcile(session, frame, contract):
    """Fail the transaction on any disagreement with the source, including payments."""
    expected_ids = {identity('invoice', value) for value in frame.Document_No}
    invoices = (await session.scalars(select(Invoice))).all()
    if {invoice.id for invoice in invoices} != expected_ids:
        raise ValueError('Database invoice identities differ from the source corpus')
    by_id = {invoice.id: invoice for invoice in invoices}
    expected_companies = {identity('customer', int(value)) for value in frame.Cust_Num}
    actual_companies = set((await session.scalars(select(Company.id))).all())
    if actual_companies != expected_companies:
        raise ValueError('Database customer identities differ from the source corpus')
    payments = (await session.scalars(select(Payment))).all()
    events = (await session.scalars(select(PaymentEvent))).all()
    if {p.id for p in payments} != {identity('payment', v) for v in frame.Document_No}:
        raise ValueError('Payment identities differ from source clearing records')
    if {p.id for p in events} != {identity('payment_event', v) for v in frame.Document_No}:
        raise ValueError('Payment event identities differ from source clearing records')
    payment_by_id = {p.invoice_id: p for p in payments}
    event_by_id = {event.invoice_id: event for event in events}
    for row in frame.itertuples(index=False):
        invoice = by_id[identity('invoice', row.Document_No)]
        payment = payment_by_id[invoice.id]
        event = event_by_id[invoice.id]
        if (invoice.company_id != identity('customer', int(row.Cust_Num))
                or invoice.invoice_number != f'B2B-{row.Document_No}'
                or invoice.amount_total != row.Amount or invoice.amount_paid != row.Amount
                or invoice.posting_date != row.Posting_Date.date()
                or invoice.issue_date != row.Doc_Date.date() or invoice.due_date != row.Net_Due_Date.date()
                or invoice.payment_term != int(row.Payment_Term)
                or invoice.status != InvoiceStatus.PAID or invoice.currency != 'INR'
                or payment.amount != row.Amount or payment.payment_date.date() != row.Clearing_date.date()
                or payment.status != PaymentStatus.SUCCESS or payment.currency != 'INR'
                or event.event_type != PaymentEventType.PAYMENT_RECEIVED
                or event.occurred_at.date() != row.Clearing_date.date()
                or event.payload.get('source') != 'public_b2b_dataset'
                or Decimal(str(event.payload.get('amount'))) != row.Amount
                or event.payload.get('clearing_date') != row.Clearing_date.date().isoformat()
                or invoice.payment_risk_model_version != contract['version']
                or invoice.payment_risk_artifact_sha256 != contract['artifact_sha256']
                or invoice.late_payment_probability is None or invoice.payment_risk_scored_at is None
                or not invoice.payment_risk_features or not invoice.payment_risk_drivers
                or not invoice.payment_risk_explanation_metadata):
            raise ValueError('Source or scoring reconciliation failed; existing records were not overwritten')
    amount = sum((invoice.amount_total for invoice in invoices), Decimal('0'))
    if amount != sum(frame.Amount, Decimal('0')) or sum((p.amount for p in payments), Decimal('0')) != amount:
        raise ValueError('Exact invoice/payment amount reconciliation failed')
    return {**dataset_metadata(frame), 'database_invoices': len(invoices),
            'database_customers': len(actual_companies), 'database_payments': len(payments),
            'database_payment_events': len(events), 'database_amount_total': str(amount),
            'eligible_invoices': len(frame), 'scored_invoices': len(invoices),
            'matching_artifact_hashes': len(invoices)}


def require_isolated_url(url: str) -> str:
    database = make_url(url).database or ''
    if not (database.endswith('_public') or database.endswith('_test')):
        raise ValueError('REAL_DATASET_DATABASE_URL must name a dedicated *_public or *_test database')
    return url


def persist_batch(batch, contract):
    predictions = champion_predict_many([features for _, features in batch])
    for (invoice, features), prediction in zip(batch, predictions, strict=True):
        if prediction.model_version != contract['version'] or prediction.artifact_sha256 != contract['artifact_sha256']:
            raise RuntimeError('Champion changed during import')
        apply_prediction(invoice, features, prediction)


async def load() -> None:
    configured_url = os.environ.get('REAL_DATASET_DATABASE_URL')
    if not configured_url:
        raise RuntimeError('Set REAL_DATASET_DATABASE_URL to a migrated dedicated *_public or *_test PostgreSQL database; development data will not be reset')
    url = require_isolated_url(configured_url)
    source_path = resolve_dataset_path()
    frame = read_dataset(source_path)
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    contract = verified_contract()  # Fail before any writes or deserialization.
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            # Serialize concurrent imports; deterministic IDs make reruns idempotent.
            await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 458390069})
            existing_rows = (await session.scalars(select(Invoice))).all()
            existing = {invoice.id for invoice in existing_rows}
            expected = {identity('invoice', value) for value in frame.Document_No}
            if existing - expected:
                raise ValueError('Target contains unrelated invoices; refusing to mix or reset data')
            imported = await session.get(DatasetImport, 'public_b2b_dataset')
            if imported and imported.source_sha256 != source_hash:
                raise ValueError('Imported source fingerprint changed; refusing to overwrite')
            companies = set((await session.scalars(select(Company.id))).all())
            ranked = {customer: index + 1 for index, customer in enumerate(sorted(frame.Cust_Num.unique(), key=lambda v: hashlib.sha256(str(v).encode()).hexdigest()))}
            scored = 0
            batch = []
            for row, features in posting_features(frame, existing_ids=existing):
                customer = int(row.Cust_Num)
                cid = identity('customer', customer)
                iid = identity('invoice', row.Document_No)
                posting, due, clearing = row.Posting_Date.date(), row.Net_Due_Date.date(), row.Clearing_date.date()
                if cid not in companies:
                    session.add(Company(id=cid, name=row.Customer_Name if os.getenv('REAL_NAMES') == 'true' else f'Customer {ranked[customer]:04d}', segment=CompanySegment.UNKNOWN))
                    companies.add(cid)
                    await session.flush()
                if iid not in existing:
                    invoice = Invoice(id=iid, company_id=cid, invoice_number=f'B2B-{row.Document_No}',
                        amount_total=row.Amount, amount_paid=row.Amount, currency='INR',
                        issue_date=row.Doc_Date.date(), posting_date=posting, due_date=due,
                        payment_term=int(row.Payment_Term), payment_method_description=row.Payment_Method_description,
                        status=InvoiceStatus.PAID)
                    batch.append((invoice, features))
                    if len(batch) == 512:
                        persist_batch(batch, contract)
                        batch.clear()
                        await session.flush()
                    session.add(invoice)
                    paid_at = datetime.combine(clearing, datetime.min.time(), tzinfo=timezone.utc)
                    session.add(Payment(id=identity('payment', row.Document_No), invoice_id=iid, amount=row.Amount, currency='INR', payment_date=paid_at, method=PaymentMethod.OTHER, status=PaymentStatus.SUCCESS, reference_note='Public dataset clearing record'))
                    session.add(PaymentEvent(id=identity('payment_event', row.Document_No), invoice_id=iid, event_type=PaymentEventType.PAYMENT_RECEIVED, occurred_at=paid_at, payload={'source': 'public_b2b_dataset', 'amount': str(row.Amount), 'clearing_date': clearing.isoformat()}))
                    existing.add(iid)
                    scored += 1
            persist_batch(batch, contract)
            await session.flush()
            result = await reconcile(session, frame, contract)
            if verified_contract()['version'] != contract['version']:
                raise RuntimeError('Champion changed during import')
            if imported is None:
                session.add(DatasetImport(source='public_b2b_dataset', source_sha256=source_hash,
                    dataset_as_of=datetime.fromisoformat(result['dataset_as_of']).date(),
                    row_count=len(frame), customer_count=result['customer_count'],
                    amount_total=Decimal(result['amount_total']), reconciled_at=datetime.now(timezone.utc)))
            await import_bpi_evaluations(session)
        # Report only after the transaction committed successfully.
        print({**result, 'new_predictions': scored, 'model_version': contract['version'],
               'artifact_sha256': contract['artifact_sha256'], 'source_sha256': source_hash})
        # Real corpus is entirely cleared at its extraction cutoff. Run the
        # existing sweep and report its genuine empty result, without inventing cases.
        from app.services.risk_engine import run_detection
        async with factory() as session:
            workflow = await run_detection(session, today=datetime.fromisoformat(result['dataset_as_of']).date())
            print({'workflow_as_of': result['dataset_as_of'],
                   'mode': 'simulated workflow on real invoices',
                   'invoices_marked_overdue': len(workflow.invoices_marked_overdue),
                   'cases_created': len(workflow.cases_created),
                   'outbound_actions_executed': 0})
    finally:
        await engine.dispose()


def verify_inference():
    """Full real inference without database writes, useful before provisioning storage."""
    frame = read_dataset()
    contract = verified_contract()
    batch = []
    scored = 0
    for _, features in posting_features(frame):
        batch.append(features)
        if len(batch) == 512:
            predictions = champion_predict_many(batch)
            if any(p.artifact_sha256 != contract['artifact_sha256'] or p.model_version != contract['version'] for p in predictions):
                raise RuntimeError('Model provenance changed during full inference verification')
            scored += len(predictions)
            batch.clear()
    predictions = champion_predict_many(batch)
    if any(p.artifact_sha256 != contract['artifact_sha256'] or p.model_version != contract['version'] for p in predictions):
        raise RuntimeError('Model provenance changed during full inference verification')
    scored += len(predictions)
    if scored != len(frame):
        raise RuntimeError('Incomplete eligible-invoice scoring')
    print({'eligible_invoices': len(frame), 'actual_inferences': scored,
           'matching_artifact_hashes': scored, 'valid_shap_explanations': scored,
           'artifact_sha256': contract['artifact_sha256'], 'model_version': contract['version'],
           'persisted': False})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-source', action='store_true')
    parser.add_argument('--verify-inference', action='store_true')
    args = parser.parse_args()
    if args.verify_source:
        print(dataset_metadata(read_dataset()))
    elif args.verify_inference:
        verify_inference()
    else:
        asyncio.run(load())
