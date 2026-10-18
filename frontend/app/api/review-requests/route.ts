
import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { backendBase } from '../../../lib/backend-base';

export const dynamic = 'force-dynamic';

const id = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;

async function handle(req: NextRequest, method: 'GET' | 'POST') {
  const token = (await cookies()).get('invoiceiq_session')?.value;

  if (!token) {
    return Response.json(
      { detail: 'Sign in to workspace first' },
      { status: 401 }
    );
  }

  let body: string | undefined;

  const headers: Record<string, string> = {
    Authorization: 'Bearer ' + token
  };

  if (method === 'POST') {
    if (req.headers.get('origin') !== req.nextUrl.origin) {
      return Response.json(
        { detail: 'Invalid origin' },
        { status: 403 }
      );
    }

    if (!(req.headers.get('content-type') || '')
      .startsWith('application/json')) {
      return Response.json(
        { detail: 'JSON required' },
        { status: 415 }
      );
    }

    let data: unknown;

    try {
      data = await req.json();
    } catch {
      return Response.json(
        { detail: 'Invalid JSON' },
        { status: 400 }
      );
    }

    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return Response.json(
        { detail: 'Invalid data' },
        { status: 422 }
      );
    }

    const d = data as Record<string, unknown>;

    if (
      !id.test(String(d.case_id || '')) ||
      typeof d.note !== 'string' ||
      d.note.trim().length < 3 ||
      d.note.length > 1000 ||
      Object.keys(d).some(
        k => !['case_id', 'note'].includes(k)
      )
    ) {
      return Response.json(
        { detail: 'Valid case ID and reason required' },
        { status: 422 }
      );
    }

    const key = req.headers.get('x-invoiceiq-reviewer-key');

    if (!key) {
      return Response.json(
        { detail: 'Reviewer key required' },
        { status: 401 }
      );
    }

    headers['X-InvoiceIQ-Reviewer-Key'] = key;
    headers['Content-Type'] = 'application/json';

    if (process.env.INVOICEIQ_OPERATOR_API_KEY) {
      headers['X-InvoiceIQ-Operator-Key'] =
        process.env.INVOICEIQ_OPERATOR_API_KEY;
    }

    body = JSON.stringify({
      case_id: d.case_id,
      note: d.note.trim()
    });
  }

  const url = backendBase() + '/dashboard/workspace/' +
    (method === 'POST' ? 'review-requests' : 'review-candidates');

  try {
    const response = await fetch(url, {
      method,
      headers,
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(30000)
    });

    return new Response(response.body, {
      status: response.status,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store'
      }
    });
  } catch {
    return Response.json(
      { detail: 'Recovery API unavailable' },
      { status: 503 }
    );
  }
}

export async function GET(req: NextRequest) {
  return handle(req, 'GET');
}

export async function POST(req: NextRequest) {
  return handle(req, 'POST');
}
