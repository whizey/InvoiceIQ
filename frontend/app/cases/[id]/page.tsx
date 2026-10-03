import Link from "next/link";
import { notFound } from "next/navigation";
import { ActionButton } from "@/components/ActionButton";
import { Badge } from "@/components/Badge";
import { AuditTimeline } from "@/components/AuditTimeline";
import { Field, Section } from "@/components/Section";
import { runRecoveryCaseAction, simulatePaymentAction } from "@/app/actions";
import { SendReminderEmailButton } from "@/components/SendReminderEmailButton";
import { VoiceCallPanel } from "@/components/VoiceCallPanel";
import { RiskExplanationPanel } from "@/components/RiskExplanationPanel";
import { ApiError, getRecoveryCase, getRiskExplanation } from "@/lib/api";
import {
  actionStatusTone,
  caseStatusTone,
  policyDecisionTone,
  promiseStatusTone,
  riskTone,
} from "@/lib/badges";
import { formatCurrency, formatDate, formatDateTime, formatPercent, titleCase } from "@/lib/format";

const TERMINAL_STATUSES = new Set(["CLOSED", "CLOSED_UNRECOVERED", "RECOVERED"]);

export default async function CaseDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let caseDetail;
  try {
    caseDetail = await getRecoveryCase(id);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-700">
        <p className="font-semibold">Couldn&apos;t load this case.</p>
        <p className="mt-1">{err instanceof ApiError ? err.message : "Unknown error."}</p>
      </div>
    );
  }

  const riskExplanation = await getRiskExplanation(id).catch(() => null);

  const isTerminal = TERMINAL_STATUSES.has(caseDetail.status);
  const outstanding = Number(caseDetail.invoice.amount_total) - Number(caseDetail.invoice.amount_paid);

  const diagnosis = [...caseDetail.agent_decisions].reverse().find((d) => d.stage === "DIAGNOSIS");
  const recommendation = [...caseDetail.agent_decisions]
    .reverse()
    .find((d) => d.stage === "INTERVENTION_RECOMMENDATION");

  return (
    <div className="space-y-6">
      <Link href="/" className="text-sm text-indigo-600 hover:underline">
        ← Back to dashboard
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
            {caseDetail.invoice.company.name}
          </h1>
          <p className="mt-1 font-mono text-sm text-slate-500">{caseDetail.invoice.invoice_number}</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Badge tone={caseStatusTone(caseDetail.status)}>{titleCase(caseDetail.status)}</Badge>
            {caseDetail.risk_level ? (
              <Badge tone={riskTone(caseDetail.risk_level)}>{caseDetail.risk_level} risk</Badge>
            ) : (
              <Badge tone="gray">Unscored</Badge>
            )}
            {caseDetail.recovery_probability && (
              <span className="text-xs text-slate-400">
                {formatPercent(Number(caseDetail.recovery_probability))} recovery probability
              </span>
            )}
          </div>
        </div>

        {!isTerminal && (
          <div className="flex flex-wrap items-start gap-3">
            <ActionButton
              label="Simulate payment"
              pendingLabel="Recording…"
              variant="secondary"
              onRun={simulatePaymentAction.bind(null, caseDetail.invoice.id, caseDetail.id)}
            />
            <ActionButton
              label="Run recovery cycle"
              pendingLabel="Running…"
              onRun={runRecoveryCaseAction.bind(null, caseDetail.id)}
            />
            <SendReminderEmailButton caseId={caseDetail.id} />
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-1">
          <Section title="Invoice & Company">
            <dl className="space-y-3">
              <Field label="Company" value={caseDetail.invoice.company.name} />
              <Field
                label="Segment / Industry"
                value={`${titleCase(caseDetail.invoice.company.segment)} · ${caseDetail.invoice.company.industry ?? "—"}`}
              />
              <Field label="Invoice amount" value={formatCurrency(caseDetail.invoice.amount_total)} />
              <Field label="Amount paid" value={formatCurrency(caseDetail.invoice.amount_paid)} />
              <Field label="Outstanding" value={formatCurrency(outstanding)} />
              <Field label="Due date" value={formatDate(caseDetail.invoice.due_date)} />
              <Field label="Invoice status" value={titleCase(caseDetail.invoice.status)} />
            </dl>
          </Section>

          <Section title="Risk & Recovery">
            <dl className="space-y-3">
              <Field
                label="Risk score"
                value={caseDetail.risk_score ? `${caseDetail.risk_score} / 100` : "Not yet scored"}
              />
              <Field
                label="Recovery probability"
                value={formatPercent(
                  caseDetail.recovery_probability ? Number(caseDetail.recovery_probability) : null,
                )}
              />
              <Field label="Revenue at risk" value={formatCurrency(caseDetail.revenue_at_risk)} />
              <Field
                label="Recovered amount"
                value={
                  Number(caseDetail.recovered_amount) > 0
                    ? formatCurrency(caseDetail.recovered_amount)
                    : "—"
                }
              />
              <Field label="Opened" value={formatDateTime(caseDetail.opened_at)} />
              <Field label="Recovery window ends" value={formatDate(caseDetail.recovery_window_deadline)} />
              {caseDetail.closed_at && <Field label="Closed" value={formatDateTime(caseDetail.closed_at)} />}
            </dl>
          </Section>

          <RiskExplanationPanel explanation={riskExplanation} />

          {caseDetail.promises_to_pay.length > 0 && (
            <Section title="Promise to Pay">
              <ul className="space-y-4">
                {caseDetail.promises_to_pay.map((p) => (
                  <li key={p.id} className="text-sm">
                    <div className="flex items-center gap-2">
                      <Badge tone={promiseStatusTone(p.status)}>{titleCase(p.status)}</Badge>
                      <span className="font-medium text-slate-900">
                        {formatCurrency(p.promised_amount)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-slate-500">
                      Promised by {formatDate(p.promised_date)}
                      {p.fulfilled_at && ` · fulfilled ${formatDateTime(p.fulfilled_at)}`}
                    </p>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </div>

        <div className="space-y-6 lg:col-span-2">
          <Section title="Latest Diagnosis & Recommendation">
            {diagnosis || recommendation ? (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {diagnosis && (
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                      Diagnosis
                    </p>
                    <p className="mt-1 text-sm font-medium text-slate-900">
                      {String(diagnosis.output.diagnosis ?? "")}
                    </p>
                    <p className="mt-1 text-sm text-slate-600">{diagnosis.rationale}</p>
                    <p className="mt-2 text-xs text-slate-400">
                      via <span className="font-mono">{diagnosis.model_name}</span>
                    </p>
                  </div>
                )}
                {recommendation && (
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
                      Recommended action
                    </p>
                    <p className="mt-1 text-sm font-medium text-slate-900">
                      {titleCase(String(recommendation.output.action ?? ""))}
                    </p>
                    <p className="mt-1 text-sm text-slate-600">{recommendation.rationale}</p>
                    <p className="mt-2 text-xs text-slate-400">
                      via <span className="font-mono">{recommendation.model_name}</span>
                    </p>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-slate-400">
                No AI decisions yet — run a recovery cycle to generate one.
              </p>
            )}
          </Section>

          <Section title="Action History">
            {caseDetail.actions.length === 0 ? (
              <p className="text-sm text-slate-400">No actions taken yet.</p>
            ) : (
              <ol className="space-y-4">
                {caseDetail.actions.map((action) => {
                  const overridden =
                    action.recommended_action_type !== null &&
                    action.recommended_action_type !== action.action_type;
                  return (
                    <li key={action.id} className="rounded-lg border border-slate-100 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs font-semibold text-slate-400">
                          #{action.sequence_number}
                        </span>
                        <span className="text-sm font-medium text-slate-900">
                          {titleCase(action.action_type)}
                        </span>
                        <Badge tone={actionStatusTone(action.status)}>{titleCase(action.status)}</Badge>
                        {action.executed_at && (
                          <span className="text-xs text-slate-400">
                            {formatDateTime(action.executed_at)}
                          </span>
                        )}
                      </div>
                      {overridden && (
                        <p className="mt-2 text-xs text-amber-700">
                          AI suggested{" "}
                          <span className="font-semibold">
                            {titleCase(action.recommended_action_type!)}
                          </span>
                          {" → "}policy overrode to{" "}
                          <span className="font-semibold">{titleCase(action.action_type)}</span>
                        </p>
                      )}
                      {action.policy_decisions.map((pd) => (
                        <div key={pd.id} className="mt-2 flex items-start gap-2 text-xs">
                          <Badge tone={policyDecisionTone(pd.decision)}>{titleCase(pd.decision)}</Badge>
                          <span className="text-slate-500">{pd.reason}</span>
                        </div>
                      ))}
                    </li>
                  );
                })}
              </ol>
            )}
          </Section>

          {!isTerminal && (
            <Section title="Hinglish Recovery Call">
              <VoiceCallPanel
                caseId={caseDetail.id}
                disabled={caseDetail.status === "ESCALATED"}
                disabledReason="case is already escalated to a human — evaluate_policy() blocks new calls."
              />
            </Section>
          )}

          {caseDetail.communication_logs.length > 0 && (
            <Section title="Communications">
              <ul className="space-y-3">
                {caseDetail.communication_logs.map((log) => (
                  <li key={log.id} className="rounded-lg border border-slate-100 p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={log.channel === "VOICE" ? "violet" : "gray"}>{titleCase(log.channel)}</Badge>
                      <Badge tone={log.direction === "OUTBOUND" ? "blue" : "violet"}>
                        {titleCase(log.direction)}
                      </Badge>
                      <span className="font-medium text-slate-900">{log.subject ?? "(no subject)"}</span>
                      {log.sent_at && (
                        <span className="text-xs text-slate-400">{formatDateTime(log.sent_at)}</span>
                      )}
                    </div>
                    {log.recipient_email && (
                      <p className="mt-1 text-xs text-slate-500">
                        Delivered to <span className="font-mono">{log.recipient_email}</span>
                      </p>
                    )}
                    {log.body && <p className="mt-1 text-slate-600">{log.body}</p>}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="Audit Trail">
            <AuditTimeline entries={caseDetail.audit_logs} />
          </Section>
        </div>
      </div>
    </div>
  );
}
