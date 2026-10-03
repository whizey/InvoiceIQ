import { ActionButton } from "@/components/ActionButton";
import { BreakdownList } from "@/components/BreakdownList";
import { CasesTable } from "@/components/CasesTable";
import { MetricCard } from "@/components/MetricCard";
import { Section } from "@/components/Section";
import { detectOverdueAction } from "@/app/actions";
import { riskTone } from "@/lib/badges";
import { ApiError, getDashboardMetrics, getPolicyOverrideStats, listRecoveryCases } from "@/lib/api";
import { formatNumber, formatCurrency, formatPercent, humanizeActionType, humanizeRule } from "@/lib/format";
import type { RecoveryActionType } from "@/lib/types";

const RISK_ORDER = ["LOW", "MEDIUM", "HIGH", "UNSCORED"] as const;

const ACTION_TONE: Record<RecoveryActionType, "blue" | "violet" | "orange" | "gray"> = {
  SEND_EMAIL: "blue",
  SEND_PAYMENT_LINK: "blue",
  TRACK_PROMISE_TO_PAY: "violet",
  ESCALATE: "orange",
  WAIT: "gray",
  CLOSE_CASE: "gray",
  PLACE_VOICE_CALL: "violet",
};

export default async function DashboardPage() {
  let metrics;
  let cases;
  let overrideStats;
  try {
    [metrics, cases, overrideStats] = await Promise.all([
      getDashboardMetrics(),
      listRecoveryCases(),
      getPolicyOverrideStats(),
    ]);
  } catch (err) {
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-700">
        <p className="font-semibold">Couldn&apos;t load the dashboard.</p>
        <p className="mt-1">{err instanceof ApiError ? err.message : "Unknown error."}</p>
      </div>
    );
  }

  const riskItems = RISK_ORDER.map((level) => ({
    label: level,
    count: metrics.cases_by_risk_level[level] ?? 0,
    tone: riskTone(level === "UNSCORED" ? null : level),
  }));

  const actionCounts = new Map<string, number>();
  for (const c of cases) {
    if (!c.current_action) continue;
    actionCounts.set(c.current_action, (actionCounts.get(c.current_action) ?? 0) + 1);
  }
  const actionItems = Array.from(actionCounts.entries()).map(([action, count]) => ({
    label: humanizeActionType(action),
    count,
    tone: ACTION_TONE[action as RecoveryActionType] ?? "gray",
  }));

  const example = overrideStats.examples[0];

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-2xl">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
            Revenue Recovery Dashboard
          </h1>
          <p className="mt-2 text-sm text-slate-600">
            This demo watches overdue B2B invoices and suggests what to do next — the AI recommends,
            but a fixed set of rules can change its mind before anything actually happens. All data on
            this page is synthetic, not real customers.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <ActionButton
            label="Find newly overdue invoices"
            pendingLabel="Checking…"
            onRun={detectOverdueAction}
            variant="secondary"
          />
          <p className="max-w-[16rem] text-right text-xs text-slate-400">
            This already runs on its own in the background — click only to trigger it right now.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricCard label="Total Revenue at Risk" value={formatCurrency(metrics.total_revenue_at_risk)} />
        <MetricCard
          label="Total Revenue Recovered"
          value={formatCurrency(metrics.total_revenue_recovered)}
          tone="success"
        />
        <MetricCard
          label="Recovery Rate"
          value={formatPercent(metrics.recovery_rate)}
          caption="Recovered ÷ (recovered + still at risk)"
        />
      </div>

      <div>
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-900">Cases That Need Work</h2>
          <p className="text-xs text-slate-500">
            {metrics.active_cases} active
            {metrics.escalated_cases > 0 && (
              <span className="text-rose-600"> · {metrics.escalated_cases} escalated</span>
            )}
          </p>
        </div>
        <CasesTable cases={cases} />
        {metrics.average_days_overdue !== null && (
          <p className="mt-2 text-xs text-slate-400">
            Averaging {formatNumber(metrics.average_days_overdue, 1)} days overdue across active cases.
          </p>
        )}
      </div>

      <Section title="When Rules Overrule the AI">
        <div className="space-y-4">
          <p className="text-2xl font-semibold tracking-tight text-slate-900">
            {overrideStats.override_count} of {overrideStats.total_evaluated}
            <span className="ml-2 text-base font-normal text-slate-500">AI suggestions changed</span>
          </p>

          {example && (
            <p className="rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
              <span className="font-medium text-slate-900">Example: {example.company_name}</span> — AI
              suggested {humanizeActionType(example.recommended_action_type)}. {humanizeRule(example.rule ?? "")}
            </p>
          )}

          {Object.keys(overrideStats.by_rule).length > 0 && (
            <ul className="space-y-2 border-t border-slate-100 pt-4 text-sm text-slate-600">
              {Object.entries(overrideStats.by_rule).map(([rule, count]) => (
                <li key={rule} className="flex items-start justify-between gap-4">
                  <span>{humanizeRule(rule)}</span>
                  <span className="shrink-0 font-semibold tabular-nums text-slate-900">{count}×</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BreakdownList title="Cases by Risk Level" items={riskItems} />
        <BreakdownList title="Recovery by Current Action" items={actionItems} />
      </div>
    </div>
  );
}
