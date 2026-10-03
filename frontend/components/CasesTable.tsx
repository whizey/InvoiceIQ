import Link from "next/link";
import { Badge } from "@/components/Badge";
import { caseStatusTone, riskTone } from "@/lib/badges";
import { formatCurrency, formatPercent, humanizeActionType, titleCase } from "@/lib/format";
import type { RecoveryCaseListItem } from "@/lib/types";

export function CasesTable({ cases }: { cases: RecoveryCaseListItem[] }) {
  if (cases.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">
        No recovery cases yet. Run a detection sweep to create some from overdue invoices.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50">
          <tr>
            {[
              "Company",
              "Invoice",
              "Amount",
              "Days Overdue",
              "Risk",
              "Status",
              "Current Action",
              "Recovered",
            ].map((h) => (
              <th
                key={h}
                scope="col"
                className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {cases.map((c) => (
            <tr key={c.id} className="transition-colors hover:bg-slate-50">
              <td className="px-4 py-3">
                <Link
                  href={`/cases/${c.id}`}
                  className="font-medium text-slate-900 hover:text-indigo-600 hover:underline"
                >
                  {c.company_name}
                </Link>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-600">{c.invoice_number}</td>
              <td className="px-4 py-3 tabular-nums text-slate-900">
                {formatCurrency(c.amount_total)}
              </td>
              <td className="px-4 py-3 tabular-nums text-slate-600">{c.days_overdue}d</td>
              <td className="px-4 py-3">
                {c.risk_level ? (
                  <Badge tone={riskTone(c.risk_level)}>{titleCase(c.risk_level)}</Badge>
                ) : (
                  <Badge tone="gray">Unscored</Badge>
                )}
                {c.recovery_probability && (
                  <span className="ml-2 text-xs text-slate-400">
                    {formatPercent(Number(c.recovery_probability))}
                  </span>
                )}
              </td>
              <td className="px-4 py-3">
                <Badge tone={caseStatusTone(c.status)}>{titleCase(c.status)}</Badge>
              </td>
              <td className="px-4 py-3 text-slate-600">
                {c.current_action ? humanizeActionType(c.current_action) : "—"}
              </td>
              <td className="px-4 py-3 tabular-nums text-slate-900">
                {Number(c.recovered_amount) > 0 ? formatCurrency(c.recovered_amount) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
