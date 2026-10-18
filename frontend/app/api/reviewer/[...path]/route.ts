import { cookies } from "next/headers";
import { NextRequest } from "next/server";
import { backendBase } from "../../../../lib/backend-base";

export const dynamic = "force-dynamic";
const uuid = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;
type Ctx = { params: Promise<{ path: string[] }> };

async function forward(req: NextRequest, p: string[], method: "GET" | "POST") {
  const reviewer = req.headers.get("x-invoiceiq-reviewer-key")?.trim();
  const analytics = (await cookies()).get("invoiceiq_session")?.value;
  if (!reviewer) return Response.json({detail:"Reviewer credential required"},{status:401});
  if (!analytics && process.env.INVOICEIQ_SAMPLE_SYNTHETIC !== "true") return Response.json({detail:"Analytics workspace credential required"},{status:401});
  let target: string | null = null;
  if (method === "GET" && p.length === 1 && p[0] === "history")
    target = "/dashboard/workspace/review-history";
  if (method === "POST") {
    if (req.headers.get("origin") !== req.nextUrl.origin)
      return Response.json({detail:"Cross-origin review write blocked"},{status:403});
    if (p.length === 3 && p[0] === "reviews" && uuid.test(p[1]) &&
        (p[2] === "approve" || p[2] === "reject"))
      target = `/dashboard/workspace/reviews/${p[1]}/${p[2]}`;
  }
  if (!target) return Response.json({detail:"Unknown review route"},{status:404});
  let body: string | undefined;
  if (method === "POST") {
    if (!req.headers.get("content-type")?.startsWith("application/json"))
      return Response.json({detail:"Expected JSON"},{status:415});
    let raw: unknown;
    try { raw = await req.json(); }
    catch { return Response.json({detail:"Invalid JSON"},{status:400}); }
    const note = raw && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as {note?:unknown}).note : null;
    if (typeof note !== "string" || !note.trim() || note.length > 2000)
      return Response.json({detail:"Decision reason required"},{status:422});
    body = JSON.stringify({note:note.trim()});
  }
  const headers: Record<string,string> = {
    Authorization: `Bearer ${reviewer}`,
    ...(analytics ? { "X-InvoiceIQ-Analytics-Key": analytics } : {})
  };
  if (body) headers["Content-Type"] = "application/json";
  try {
    const response = await fetch(backendBase() + target,{
      method,headers,body,cache:"no-store",
      signal:AbortSignal.any([req.signal,AbortSignal.timeout(30000)])
    });
    return new Response(response.body,{status:response.status,
      headers:{"Content-Type":response.headers.get("content-type") || "application/json",
               "Cache-Control":"no-store"}});
  } catch {
    return Response.json({detail:"Review backend unavailable"},{status:503});
  }
}
export async function GET(req:NextRequest, ctx:Ctx) {
  return forward(req,(await ctx.params).path,"GET");
}
export async function POST(req:NextRequest, ctx:Ctx) {
  return forward(req,(await ctx.params).path,"POST");
}
