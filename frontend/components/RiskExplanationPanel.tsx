import { Section } from "@/components/Section";
import { formatCurrency, formatPercent } from "@/lib/format";
import type {
  RiskExplanation,
  RiskFeatureContribution,
} from "@/lib/types";


function formatFeatureValue(feature: string, value: number): string {
  if (feature === "invoice_amount" || feature === "outstanding_balance") {
    return formatCurrency(value);
  }

  if (feature === "prior_recovery_success_rate") {
    return formatPercent(value);
  }

  if (feature === "days_overdue" || feature === "customer_tenure_days") {
    return `${Math.round(value)} days`;
  }

  if (Number.isInteger(value)) {
    return String(value);
  }

  return value.toFixed(2);
}


function contributionText(
  contribution: RiskFeatureContribution,
): string {
  return contribution.direction === "increases_recovery_probability"
    ? "Pushes prediction toward recovery"
    : "Pushes prediction away from recovery";
}


export function RiskExplanationPanel({
  explanation,
}: {
  explanation: RiskExplanation | null;
}) {
  if (!explanation) {
    return null;
  }

  const visible = explanation.contributions.slice(0, 6);

  const maxMagnitude = Math.max(
    ...visible.map((item) => Math.abs(item.shap_value)),
    0.000001,
  );

  return (
    <Section title="Why this risk score?">
      <div className="space-y-5">
        <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
          <div className="grid grid-cols-3 gap-3 text-center">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                Risk score
              </p>
              <p className="mt-1 text-lg font-semibold text-slate-900">
                {explanation.risk_score.toFixed(2)}
              </p>
            </div>

            <div>
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                Recovery
              </p>
              <p className="mt-1 text-lg font-semibold text-slate-900">
                {formatPercent(explanation.recovery_probability)}
              </p>
            </div>

            <div>
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                Risk
              </p>
              <p className="mt-1 text-lg font-semibold text-slate-900">
                {explanation.risk_level}
              </p>
            </div>
          </div>
        </div>

        <div className="space-y-4">
          {visible.map((item) => {
            const magnitude = Math.abs(item.shap_value);
            const width = Math.max(
              4,
              (magnitude / maxMagnitude) * 100,
            );

            const positive =
              item.direction === "increases_recovery_probability";

            return (
              <div key={item.feature}>
                <div className="mb-1 flex items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-slate-800">
                      {item.label}
                    </p>

                    <p className="text-xs text-slate-400">
                      {formatFeatureValue(item.feature, item.value)}
                    </p>
                  </div>

                  <div className="text-right">
                    <p
                      className={`text-xs font-semibold ${
                        positive
                          ? "text-emerald-600"
                          : "text-rose-600"
                      }`}
                    >
                      {item.shap_value > 0 ? "+" : ""}
                      {item.shap_value.toFixed(3)}
                    </p>

                    <p className="mt-0.5 text-[10px] text-slate-400">
                      SHAP
                    </p>
                  </div>
                </div>

                <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className={`h-full rounded-full ${
                      positive
                        ? "bg-emerald-500"
                        : "bg-rose-500"
                    }`}
                    style={{ width: `${width}%` }}
                  />
                </div>

                <p className="mt-1 text-[11px] text-slate-500">
                  {contributionText(item)}
                </p>
              </div>
            );
          })}
        </div>

        <div className="border-t border-slate-100 pt-3">
          <p className="text-[11px] leading-5 text-slate-400">
            Local explanation from the XGBoost recovery model using SHAP.
            Bar length represents relative SHAP magnitude for this prediction;
            SHAP values are model-output contributions, not percentage-point
            changes in recovery probability.
          </p>
        </div>
      </div>
    </Section>
  );
}
