"""Map one B2B late-payment prediction to case priority."""

from decimal import Decimal

from app.models.enums import RiskLevel


def apply_invoice_risk_to_case(invoice, case):
    probability = invoice.late_payment_probability

    if probability is None:
        case.risk_score = None
        case.risk_level = None
    else:
        probability = float(probability)
        if not 0 <= probability <= 1:
            raise ValueError("Invalid late-payment probability")

        case.risk_score = Decimal(
            str(round(probability * 100, 2))
        )

        # Provisional prioritization thresholds.
        # These are not calibrated recovery probabilities.
        if probability >= 0.70:
            case.risk_level = RiskLevel.HIGH
        elif probability >= 0.35:
            case.risk_level = RiskLevel.MEDIUM
        else:
            case.risk_level = RiskLevel.LOW

    case.recovery_probability = None
    return case
