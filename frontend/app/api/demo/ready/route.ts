import { backendBase } from "../../../../lib/backend-base";

export const dynamic = "force-dynamic";

export async function GET() {
  if (process.env.INVOICEIQ_DEMO_SYNTHETIC !== "true") {
    return Response.json({ ready: false }, { status: 404 });
  }
  try {
    const upstream = await fetch(backendBase() + "/dashboard/calendar", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!upstream.ok) throw new Error("Synthetic demo backend is not ready");
    const data = await upstream.json();
    if (data?.data_mode !== "synthetic_demo" || data?.llm_mode !== "deterministic_fallback") {
      throw new Error("Synthetic demo provenance is unavailable");
    }
    return Response.json({ ready: true }, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ ready: false }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
