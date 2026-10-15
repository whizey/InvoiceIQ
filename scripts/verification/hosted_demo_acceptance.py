"""Post-approval HTTP acceptance for the public, read-only synthetic demo.

Run with --url https://<public-render-host>. No credentials are accepted or logged.
Private API, database, browser, and session lifecycle checks require separate
authorized access and are deliberately not inferred from these public requests.
"""

import argparse
import json
import socket
from decimal import Decimal
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def public_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTPS public origin without credentials is required")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port:
        raise ValueError("Provide only the public HTTPS origin")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("A public host is required")
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("An IP address is not an approved public demo host")
    return f"https://{host}"


def request(origin: str, path: str, *, method: str = "GET", body: bytes | None = None):
    target = origin + path
    headers = {"Accept": "application/json", "Origin": origin} if method != "GET" else {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(target, data=body, method=method, headers=headers)
    try:
        response = urlopen(req, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        final = urlsplit(response.url)
        if f"{final.scheme}://{final.netloc}" != origin:
            raise RuntimeError("Request redirected outside the approved public origin")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise RuntimeError("Response exceeds acceptance size limit")
        content_type = response.headers.get("Content-Type", "")
        data = json.loads(raw) if "application/json" in content_type else raw.decode("utf-8")
        return response.status, data, response.headers


def require_status(origin: str, path: str, expected: int, **kwargs):
    status, data, headers = request(origin, path, **kwargs)
    if status != expected:
        raise RuntimeError(f"{path}: expected HTTP {expected}, received {status}")
    return data, headers


def acceptance(origin: str) -> None:
    page, _ = require_status(origin, "/", 200)
    if "InvoiceIQ" not in page:
        raise RuntimeError("Homepage does not identify InvoiceIQ")
    ready, _ = require_status(origin, "/api/demo/ready", 200)
    if ready != {"ready": True}:
        raise RuntimeError("Synthetic demo provenance/readiness is unavailable")
    dashboard, headers = require_status(origin, "/api/finance/overview?page_size=100", 200)
    if "no-store" not in headers.get("Cache-Control", ""):
        raise RuntimeError("Financial response is cacheable")
    metrics = dashboard.get("metrics", {})
    if not dashboard.get("historical_available"):
        raise RuntimeError("Current synthetic dashboard state is unavailable")
    rows = list(dashboard.get("invoices", []))
    total = dashboard.get("total", 0)
    if not isinstance(total, int) or total < 1 or total > 1000:
        raise RuntimeError("Unexpected synthetic invoice count")
    for page in range(2, (total + 99) // 100 + 1):
        part, _ = require_status(origin, f"/api/finance/overview?page_size=100&page={page}", 200)
        if part.get("metrics") != metrics or part.get("total") != total:
            raise RuntimeError("Dashboard pages disagree on KPI totals")
        rows.extend(part.get("invoices", []))
    if len(rows) != total or len({row["id"] for row in rows}) != total:
        raise RuntimeError("Synthetic invoice pagination is incomplete")
    billed = sum((Decimal(str(row["amount_total"])) for row in rows), Decimal(0))
    invoiced = sum((Decimal(str(row["amount_total"])) for row in rows if row["status"] != "CANCELLED"), Decimal(0))
    open_balance = sum((Decimal(str(row["balance"])) for row in rows), Decimal(0))
    if (invoiced <= 0 or billed != Decimal(str(metrics["billed"]))
            or invoiced != Decimal(str(metrics["invoiced"]))
            or open_balance != Decimal(str(metrics["open"]))):
        raise RuntimeError("Dashboard billed/invoiced/open totals do not reconcile with invoice rows")
    if Decimal(str(metrics["collected"])) < 0:
        raise RuntimeError("Collected amount is invalid")
    if not rows or metrics.get("company_count", 0) < 6:
        raise RuntimeError("Synthetic invoice and company scope is incomplete")
    cases, _ = require_status(origin, "/api/workspace/cases", 200)
    if not cases.get("cases"):
        raise RuntimeError("Synthetic recovery cases are unavailable")
    filtered, _ = require_status(origin, "/api/finance/overview?status=overdue", 200)
    if filtered.get("metrics") is None or filtered.get("invoices") is None:
        raise RuntimeError("Invoice filtering is unavailable")
    for path, method, body in (
        ("/api/session", "POST", b'{"token":"invalid-demo-credential"}'),
        ("/api/review-requests", "POST", b"{}"),
        ("/api/reviewer/reviews/00000000-0000-0000-0000-000000000001/approve", "POST", b"{}"),
    ):
        status, _, _ = request(origin, path, method=method, body=body)
        if status not in {401, 403}:
            raise RuntimeError(f"{path}: public write returned HTTP {status}")
    print("PASS: public HTTPS origin, synthetic readiness, dashboard reconciliation, "
          "invoice filtering, cases, and 3 denied public writes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Approved public HTTPS origin")
    args = parser.parse_args()
    try:
        acceptance(public_origin(args.url))
    except (ValueError, KeyError, TypeError, RuntimeError, URLError, socket.timeout) as error:
        parser.exit(1, f"FAIL: hosted demo acceptance: {error}\n")


if __name__ == "__main__":
    main()
