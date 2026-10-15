"""Invoice-cohort scores, distinct from invocation/latency telemetry."""
from sqlalchemy import func, select
from app.models import Invoice, InferenceRecord
from app.ml.champion_serving import verified_contract


def valid_score(contract=None):
    contract = contract if contract is not None else verified_contract()
    return func.coalesce((Invoice.late_payment_probability.between(0, 1)
        & (Invoice.payment_risk_model == 'b2b_v2')
        & (Invoice.payment_risk_model_version == contract['version'])
        & (Invoice.payment_risk_artifact_sha256 == contract['artifact_sha256'])
        & Invoice.payment_risk_scored_at.is_not(None)
        & Invoice.payment_risk_features.is_not(None)), False)


async def stored_prediction_report(db, *, company_ids, currency, start_date, end_date, contract=None):
    cohort = [Invoice.company_id.in_(company_ids), Invoice.currency == currency,
        func.coalesce(Invoice.posting_date, Invoice.issue_date).between(start_date, end_date), valid_score(contract)]
    versions = dict((await db.execute(select(Invoice.payment_risk_model_version, func.count(Invoice.id))
        .where(*cohort).group_by(Invoice.payment_risk_model_version))).all())
    tracked = select(InferenceRecord.id).where(InferenceRecord.invoice_id == Invoice.id,
        InferenceRecord.model_kind == 'b2b', InferenceRecord.status == 'SUCCEEDED',
        InferenceRecord.model_version == Invoice.payment_risk_model_version).exists()
    with_telemetry = await db.scalar(select(func.count(Invoice.id)).where(*cohort, tracked))
    total = sum(versions.values())
    return {'stored_predictions': total, 'model_versions': versions,
        'with_recorded_inference': with_telemetry, 'without_recorded_inference': total - with_telemetry,
        'definition': 'One trusted latest prediction per invoice in the posting-date cohort. Scores without recorded inference include imported batch scores; they are not telemetry requests.'}
