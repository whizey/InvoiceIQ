
import { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { backendBase } from "../../../../lib/backend-base";

export const dynamic = "force-dynamic";

const uuid =
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const parts = (await params).path;
  let dest: string | null = null;

  if (parts.length === 1 && parts[0] === "cases") {
    dest = "/dashboard/workspace/cases";
  }

  if (
    parts.length === 2 &&
    parts[0] === "cases" &&
    uuid.test(parts[1])
  ) {
    dest = "/dashboard/workspace/cases/" + parts[1];
  }

  if (parts.length === 1 && parts[0] === "reviews") {
    dest = "/dashboard/workspace/reviews";
  }

  if (!dest) {
    return Response.json(
      { detail: "Not found" }, { status: 404 }
    );
  }

  const analytics = (await cookies()).get("invoiceiq_session")?.value;

  if (!analytics && process.env.INVOICEIQ_DEMO_SYNTHETIC !== "true") {
    return Response.json(
      { detail: "Workspace authentication required" },
      { status: 401 }
    );
  }

  const reviewer = request.headers
    .get("x-invoiceiq-reviewer-key")?.trim();

  if (parts[0] === "reviews" && !reviewer) {
    return Response.json(
      { detail: "Reviewer authorization required" },
      { status: 401 }
    );
  }

  const headers: Record<string, string> =
    parts[0] === "reviews"
      ? {
          Authorization: `Bearer ${reviewer}`,
          ...(analytics ? { "X-InvoiceIQ-Analytics-Key": analytics } : {})
        }
      : analytics ? { Authorization: `Bearer ${analytics}` } : {};

  try {
    const upstream = await fetch(
      backendBase() + dest,
      {
        headers,
        cache: "no-store",
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(25000)])
      }
    );

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type":
          upstream.headers.get("content-type") ||
          "application/json",
        "cache-control": "no-store"
      }
    });
  } catch {
    return Response.json(
      { detail: "Backend unavailable" },
      { status: 503 }
    );
  }
}
