import Link from "next/link";
import {cookies} from "next/headers";
import PortalIntelligence from "@/components/PortalIntelligence";

type Model = {
  id: string;
  role: string;
  dataset: string;
  target: string;
  algorithm: string;
  version: string | null;
  available: boolean;
  linked_to_recovery: boolean;
  limitation: string;
};

type Catalog = {
  primary_model: string | null;
  models: Model[];
};

type Landmark = {
  age: number;
  horizon: number;
  at_risk: number;
  auc: number | null;
  brier: number | null;
  predicted_mean: number | null;
  observed_rate: number | null;
};

type Report = {
  status: string;
  fold: string;
  rows: Landmark[];
};

const API =
  process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

async function getData<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }

  return response.json() as Promise<T>;
}

const pct = (value: number | null) =>
  value === null ? "N/A" : `${(value * 100).toFixed(1)}%`;

export default async function ModelIntelligencePage() {
  if((await cookies()).get("invoiceiq_portal_session")?.value){
    return <PortalIntelligence/>;
  }
  let catalog: Catalog;
  let report: Report;

  try {
    [catalog, report] = await Promise.all([
      getData<Catalog>("/ml/models"),
      getData<Report>("/ml/process-survival/landmarks"),
    ]);
  } catch {
    return (
      <main className="route-error" role="alert">
        <p className="settings-eyebrow">InvoiceIQ / Model intelligence</p>
        <h1>Model intelligence is unavailable.</h1>
        <p>The model service did not respond. Reload in a moment; no results are shown until real model output is available.</p>
        <div><Link href="/home">Return home</Link></div>
      </main>
    );
  }

  return (
    <main className="space-y-8">
      <header>
        <Link href="/" className="text-sm text-slate-500">
          ← Recovery dashboard
        </Link>

        <h1 className="mt-3 text-3xl font-semibold">
          Model Intelligence
        </h1>

        <p className="mt-2 text-sm text-slate-600">
          B2B payment-risk prediction powers recovery investigations.
          BPI 2019 survival analysis forecasts purchase-to-pay
          process clearance. Their probabilities are not combined.
        </p>
      </header>

      <section className="grid gap-4 md:grid-cols-2">
        {catalog.models.map((model) => (
          <article
            key={model.id}
            className="rounded-xl border bg-white p-5 shadow-sm"
          >
            <div className="flex justify-between gap-2">
              <h2 className="text-lg font-semibold">
                {model.role === "primary"
                  ? "B2B Payment Risk"
                  : "BPI 2019 Process Survival"}
              </h2>
              <span className="text-xs font-medium text-slate-500">
                {model.role === "primary"
                  ? "PRIMARY"
                  : "RESEARCH"}
              </span>
            </div>

            <dl className="mt-4 space-y-2 text-sm">
              <div>
                <dt className="text-slate-500">Dataset</dt>
                <dd>{model.dataset}</dd>
              </div>

              <div>
                <dt className="text-slate-500">Algorithm</dt>
                <dd>{model.algorithm}</dd>
              </div>

              <div>
                <dt className="text-slate-500">Prediction target</dt>
                <dd className="break-all font-mono text-xs">
                  {model.target}
                </dd>
              </div>

              <div>
                <dt className="text-slate-500">Model version</dt>
                <dd className="break-all text-xs">
                  {model.version ?? "Not configured"}
                </dd>
              </div>

              <div>
                <dt className="text-slate-500">Artifact</dt>
                <dd>
                  {model.available ? "Available" : "Unavailable"}
                </dd>
              </div>

              <div>
                <dt className="text-slate-500">Recovery integration</dt>
                <dd>
                  {model.linked_to_recovery
                    ? "RecoveryCase + LangGraph"
                    : "No — research analytics only"}
                </dd>
              </div>
            </dl>

            <p className="mt-4 rounded-md bg-amber-50 p-3 text-xs text-amber-900">
              {model.limitation}
            </p>
          </article>
        ))}
      </section>

      <section className="rounded-xl border bg-white p-5 shadow-sm">
        <h2 className="text-xl font-semibold">
          BPI Survival · Landmark Validation
        </h2>

        <p className="mt-2 text-sm text-slate-600">
          Historical Fold 0. Predicted versus observed
          process-clearance probability for invoices still
          uncleared at the landmark age.
        </p>

        {report.rows.length === 0 ? (
          <p className="mt-4 text-sm text-slate-500">
            No local landmark validation report available.
          </p>
        ) : (
          <div className="mt-5 overflow-x-auto">
            <table className="w-full min-w-[600px] text-left text-sm">
              <thead className="border-b text-slate-500">
                <tr>
                  <th className="p-2">Age</th>
                  <th className="p-2">Horizon</th>
                  <th className="p-2">Scored</th>
                  <th className="p-2">AUC</th>
                  <th className="p-2">Predicted</th>
                  <th className="p-2">Observed</th>
                  <th className="p-2">Gap</th>
                </tr>
              </thead>

              <tbody>
                {report.rows.map((row) => {
                  const gap =
                    row.predicted_mean === null ||
                    row.observed_rate === null
                      ? null
                      : (row.predicted_mean -
                          row.observed_rate) * 100;

                  return (
                    <tr
                      key={`${row.age}-${row.horizon}`}
                      className="border-b"
                    >
                      <td className="p-2">{row.age}d</td>
                      <td className="p-2">Next {row.horizon}d</td>
                      <td className="p-2">
                        {row.at_risk.toLocaleString()}
                      </td>
                      <td className="p-2">
                        {row.auc?.toFixed(3) ?? "N/A"}
                      </td>
                      <td className="p-2">
                        {pct(row.predicted_mean)}
                      </td>
                      <td className="p-2">
                        {pct(row.observed_rate)}
                      </td>
                      <td className="p-2">
                        {gap === null
                          ? "N/A"
                          : `${gap > 0 ? "+" : ""}${gap.toFixed(1)} pp`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-4 text-xs text-slate-500">
          Experimental research results. Process clearance
          is not customer payment or debt recovery.
        </p>
      </section>
    </main>
  );
}
