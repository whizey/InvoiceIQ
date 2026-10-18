"""
RecoverIQ — Model Comparison / Answerability Contract

Creates machine-readable knowledge for the agent so it can answer:
- Which models were compared?
- Which model won each task?
- Why did it win?
- Why wasn't XGBoost/LightGBM/RF used everywhere?
- What metrics were used?
- Which outputs are calibrated?
- Which models are experimental?
- What questions can/cannot RecoverIQ answer?

No test split is read.
"""

from __future__ import annotations

import json
from pathlib import Path


BASE = Path(
    "app/ml/artifacts/b2b_v2"
)

OUTPUT = (
    BASE
    / "model_comparison_contract.json"
)

CAPABILITY_OUTPUT = (
    BASE
    / "agent_capability_contract.json"
)


def main():

    # ---------------------------------------------------------
    # Final B2B v2 model comparison
    # ---------------------------------------------------------

    comparison = {
        "evaluation_philosophy": {
            "comparison_is_fair": True,

            "reason": (
                "Models were evaluated on the same leakage-safe "
                "development data, temporal folds, target definitions "
                "and task-specific metrics."
            ),

            "selection_strategy": (
                "Temporal stability was prioritized over a single "
                "high validation score."
            ),

            "classification_metrics": [
                "median temporal ROC-AUC",
                "worst-fold ROC-AUC",
                "ROC-AUC standard deviation",
                "PR-AUC",
                "PR lift",
                "Brier score",
                "expected calibration error",
            ],

            "regression_metrics": [
                "median MAE",
                "worst-fold MAE",
                "MAE standard deviation",
                "RMSE",
                "R2",
            ],

            "quantile_metrics": [
                "pinball loss",
                "quantile coverage",
                "coverage error",
            ],

            "business_metrics": [
                "Precision@K",
                "Recall@K",
                "risky cash captured@K",
                "expected overdue exposure ranking",
            ],
        },

        "models_compared": [
            "Random Forest",
            "Gradient Boosting",
            "XGBoost",
            "LightGBM",
        ],

        "champions": {

            "late_payment": {
                "business_question":
                    "Is this invoice likely to miss its due date?",

                "champion":
                    "LightGBM",

                "feature_set":
                    "behaviour_heavy",

                "status":
                    "SUPPORTED_UNCALIBRATED",

                "output_semantics":
                    "late-payment risk score",

                "current_metrics": {
                    "median_temporal_auc":
                        0.6656,

                    "worst_temporal_auc":
                        0.6200,
                },

                "previous_champion": {
                    "model":
                        "Gradient Boosting",

                    "median_temporal_auc":
                        0.6294,

                    "worst_temporal_auc":
                        0.6063,
                },

                "why_selected": (
                    "LightGBM improved both median temporal AUC "
                    "and worst-period AUC over Gradient Boosting."
                ),

                "calibration": {
                    "method":
                        "none",

                    "reason":
                        (
                            "Sequential Platt calibration reduced "
                            "ROC-AUC and PR-AUC, so the raw model "
                            "score was retained."
                        ),
                },
            },

            "delay_over_7d": {
                "business_question":
                    "What is the probability that the invoice becomes more than 7 days late?",

                "champion":
                    "Random Forest",

                "feature_set":
                    "no_open_ratio",

                "status":
                    "SUPPORTED_CALIBRATED",

                "output_semantics":
                    "calibrated probability",

                "current_metrics": {
                    "median_temporal_auc":
                        0.7760,

                    "worst_temporal_auc":
                        0.6243,
                },

                "lightgbm_challenger": {
                    "median_temporal_auc":
                        0.7584,

                    "worst_temporal_auc":
                        0.6357,
                },

                "why_selected": (
                    "Random Forest retained stronger overall "
                    "temporal discrimination and its Platt "
                    "calibration passed the honest temporal audit."
                ),

                "calibration": {
                    "method":
                        "Platt",

                    "status":
                        "accepted",
                },
            },

            "delay_over_15d": {
                "business_question":
                    "Will the invoice become more than 15 days late?",

                "champion":
                    "Random Forest",

                "feature_set":
                    "behaviour_heavy",

                "status":
                    "EXPERIMENTAL_UNCALIBRATED",

                "median_temporal_auc":
                    0.9441,

                "worst_temporal_auc":
                    0.5080,

                "why_experimental": (
                    "The target has insufficient positive support "
                    "in later temporal periods and could not be "
                    "honestly sequentially calibrated."
                ),

                "output_semantics":
                    "experimental ranking score, not probability",
            },

            "expected_days_late": {
                "business_question":
                    "If the invoice becomes late, roughly how many days late will it be?",

                "champion":
                    "XGBoost",

                "feature_set":
                    "no_open_ratio",

                "status":
                    "SUPPORTED",

                "median_temporal_mae":
                    3.187,

                "why_selected": (
                    "XGBoost achieved lower temporal MAE than "
                    "the LightGBM challenger."
                ),
            },

            "p80_lateness": {
                "champion":
                    "Gradient Boosting Quantile",

                "status":
                    "SUPPORTED_PROVISIONAL",

                "why_selected": (
                    "It outperformed the LightGBM quantile challenger "
                    "and achieved substantially better P80 coverage."
                ),
            },
        },

        "important_explanations": {

            "why_not_one_model_everywhere": (
                "Different prediction tasks have different target "
                "distributions and loss functions. RecoverIQ therefore "
                "selects the strongest temporally stable model separately "
                "for each task."
            ),

            "why_not_lightgbm_everywhere": (
                "LightGBM improved late-payment risk but did not "
                "outperform the existing champions for lateness regression "
                "or quantile prediction, and its >7-day calibration was "
                "inferior to the Random Forest pipeline."
            ),

            "why_not_xgboost_everywhere": (
                "XGBoost was strongest for conditional lateness regression "
                "but was not the most temporally stable classifier for "
                "all binary targets."
            ),

            "why_temporal_validation": (
                "Invoices occur through time and payment behaviour changes. "
                "Random train/test splitting could leak future regimes into "
                "training and produce unrealistic performance estimates."
            ),

            "why_test_is_locked": (
                "The reserved test set is intentionally untouched while "
                "model architecture, calibration and champion selection are "
                "being finalized."
            ),
        },

        "business_value": {
            "ranking_method":
                "expected overdue exposure",

            "top_100_cash_capture":
                0.1099,

            "top_250_cash_capture":
                0.2180,

            "top_500_cash_capture":
                0.3441,

            "top_1000_cash_capture":
                0.5204,

            "interpretation": (
                "Exposure-aware prioritization captures substantially "
                "more risky cash than ranking invoices only by late risk."
            ),
        },

        "test_split_read":
            False,
    }


    # ---------------------------------------------------------
    # Agent answerability contract
    # ---------------------------------------------------------

    capabilities = {

        "supported": {

            "late_payment_risk": {
                "answer":
                    True,

                "source":
                    "B2B LightGBM",

                "note":
                    "Return as risk score rather than calibrated probability.",
            },

            "clear_by_due_date": {
                "answer":
                    True,

                "source":
                    "B2B late-payment risk head",

                "note":
                    (
                        "Because late-payment head is uncalibrated, "
                        "avoid presenting this as a precise calibrated probability."
                    ),
            },

            "delay_over_7_days": {
                "answer":
                    True,

                "source":
                    "Random Forest + Platt",

                "probability_supported":
                    True,
            },

            "expected_days_late_if_late": {
                "answer":
                    True,

                "source":
                    "XGBoost",
            },

            "p80_lateness": {
                "answer":
                    True,

                "source":
                    "Gradient Boosting Quantile",

                "status":
                    "provisional",
            },

            "invoice_priority": {
                "answer":
                    True,

                "source":
                    "risk + exposure + deterministic policy",
            },

            "cash_at_risk": {
                "answer":
                    True,

                "source":
                    "amount × late risk score",

                "note":
                    "Treat as expected-risk exposure score, not audited accounting loss.",
            },

            "intervention_recommendation": {
                "answer":
                    True,

                "source":
                    "deterministic business policy",

                "note":
                    "Not a learned causal treatment model.",
            },

            "model_comparison": {
                "answer":
                    True,

                "source":
                    "model_comparison_contract.json",
            },

            "why_model_selected": {
                "answer":
                    True,

                "source":
                    "temporal model tournament",
            },

            "model_metrics": {
                "answer":
                    True,

                "source":
                    "training/calibration/model comparison artifacts",
            },

            "calibration_status": {
                "answer":
                    True,
            },

            "feature_set_used": {
                "answer":
                    True,
            },

            "why_not_other_model": {
                "answer":
                    True,
            },
        },


        "experimental": {

            "delay_over_15_days": {
                "answer":
                    True,

                "must_disclose":
                    "Experimental and uncalibrated; limited temporal support.",
            },

        },


        "requires_bpi_survival_model": {

            "will_invoice_eventually_recover":
                True,

            "when_will_it_clear_from_today":
                True,

            "probability_clear_within_next_7_days":
                True,

            "probability_clear_within_next_30_days":
                True,

            "probability_clear_within_next_60_days":
                True,

            "probability_clear_within_next_90_days":
                True,

            "likely_clearance_window":
                True,

            "recovery_curve":
                True,

            "process_delay_risk":
                True,
        },


        "unsupported_without_new_data": {

            "did_intervention_cause_payment":
                "Requires treatment/control or intervention outcome data.",

            "optimal_contact_channel":
                "Requires historical channel/action outcome data.",

            "best_collector_for_invoice":
                "Requires collector assignment and outcome history.",

            "customer_will_never_pay":
                "Requires reliable non-recovery/default labels.",

            "causal_reason_for_late_payment":
                "Predictive feature importance does not establish causality.",
        },


        "fallback_policy": {
            "rule": (
                "If no model, tool or dataset provides evidence for the "
                "question, explicitly state that RecoverIQ cannot answer "
                "it reliably yet. Never invent a probability, date, cause "
                "or recommendation."
            )
        },
    }


    OUTPUT.write_text(
        json.dumps(
            comparison,
            indent=2,
        )
    )

    CAPABILITY_OUTPUT.write_text(
        json.dumps(
            capabilities,
            indent=2,
        )
    )

    print("=" * 80)
    print("RECOVERIQ — AGENT KNOWLEDGE CONTRACT BUILT")
    print("=" * 80)

    print()
    print("Model comparison:")
    print(" ", OUTPUT)

    print()
    print("Capability contract:")
    print(" ", CAPABILITY_OUTPUT)

    print()
    print("TEST SPLIT WAS NEVER READ.")


if __name__ == "__main__":
    main()
