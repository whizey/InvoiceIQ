import { Section } from "@/components/Section";
import { formatPercent } from "@/lib/format";
import type { RiskExplanation } from "@/lib/types";

export function RiskExplanationPanel({
  explanation,
}: {
  explanation: RiskExplanation | null;
}) {
  if (!explanation) return null;

  const drivers = explanation.contributions.slice(0, 5);
  const maxShap = Math.max(
    ...drivers.map((d) => Math.abs(d.shap_value)),
    0.000001,
  );

  return (
    <Section title="B2B late-payment prediction · SHAP">
      <div className="space-y-4">
        <p className="text-xs text-amber-700">
          Experimental historical late-payment prediction.
          This is not a recovery probability.
        </p>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <p className="text-xs text-slate-500">
              Late-payment probability
            </p>
            <p className="text-xl font-semibold">
              {explanation.late_payment_probability === null
                ? "Unavailable"
                : formatPercent(explanation.late_payment_probability)}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Case priority</p>
            <p className="text-xl font-semibold">
              {explanation.risk_level ?? "Unscored"}
            </p>
          </div>
        </div>

        <p className="break-all text-xs text-slate-500">
          Model: {explanation.model_name ?? "Unavailable"}
          {" · "}Version: {explanation.model_version ?? "Unavailable"}
        </p>

        {drivers.map((item) => {
          const higher =
            item.direction === "increases_late_payment_probability";

          return (
            <div key={item.feature} className="space-y-1">
              <div className="flex justify-between gap-3 text-sm">
                <span>{item.label}</span>
                <span className={
                  higher ? "text-rose-600" : "text-emerald-700"
                }>
                  {item.shap_value >= 0 ? "+" : ""}
                  {item.shap_value.toFixed(3)}
                </span>
              </div>
              <div className="h-2 rounded bg-slate-100">
                <div
                  className={
                    higher
                      ? "h-2 rounded bg-rose-400"
                      : "h-2 rounded bg-emerald-500"
                  }
                  style={{
                    width: `${Math.max(
                      4,
                      Math.abs(item.shap_value) / maxShap * 100
                    )}%`,
                  }}
                />
              </div>
              <p className="text-xs text-slate-500">
                {higher ? "Increases" : "Decreases"} model output
                {" · "}Feature value: {item.value ?? "Unavailable"}
              </p>
            </div>
          );
        })}

        <p className="text-xs text-slate-500">
          SHAP values are model-output contributions, not
          percentage-point changes. The score alone must not
          authorize a collection action.
        </p>
      </div>
    </Section>
  );
}
