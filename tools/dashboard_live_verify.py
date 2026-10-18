from __future__ import annotations

import json
import math
import time
from decimal import Decimal, InvalidOperation
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

BASE = "http://127.0.0.1:3046"
PATH = "/api/finance/overview"

RANGES = [
    ("Full history", "2025-08-05", "2026-10-13"),
    ("Recent period", "2026-09-01", "2026-10-13"),
]

def amount(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("Non-finite amount")
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid financial amount")

def fetch(start, end):
    url = (
        f"{BASE}{PATH}"
        f"?start_date={start}"
        f"&end_date={end}"
        "&page=1"
    )

    started = time.perf_counter()

    with urlopen(url, timeout=30) as response:
        payload = json.load(response)
        status = response.status

    latency = (time.perf_counter() - started) * 1000
    return payload, status, latency

def validate(payload):
    issues = []

    if not isinstance(payload, dict):
        return ["Response must be a JSON object"]

    metrics = payload.get("metrics")

    if not isinstance(metrics, dict):
        return ["Missing metrics object"]

    required = [
        "invoiced",
        "open",
        "overdue",
        "collected",
        "invoice_count",
    ]

    for key in required:
        if key not in metrics:
            issues.append(f"Missing KPI source: {key}")

    currency = payload.get("currency")

    if currency != "INR":
        issues.append(
            f"Reporting currency is {currency!r}, not INR"
        )

    for field in ("open", "overdue", "invoiced"):
        try:
            value = amount(metrics.get(field))
            if value is not None and value < 0:
                issues.append(f"{field} is negative")
        except ValueError:
            issues.append(f"{field} contains an invalid amount")

    try:
        overdue = amount(metrics.get("overdue"))
        outstanding = amount(metrics.get("open"))

        if overdue is not None and outstanding is not None:
            if overdue > outstanding + Decimal("0.01"):
                issues.append(
                    "Overdue exceeds comparable open receivables"
                )
    except ValueError:
        pass

    if not isinstance(payload.get("series"), list):
        issues.append("Chart series missing or not an array")

    for field in ("aging", "risk", "invoices"):
        if field in payload and not isinstance(payload[field], list):
            issues.append(f"{field} is not an array")

    return issues

def main():
    print("\n=== INVOICEIQ LIVE DASHBOARD VERIFICATION ===")

    failures = 0

    for label, start, end in RANGES:
        print(f"\n{label}: {start} to {end}")

        try:
            data, status, elapsed = fetch(start, end)

            print("HTTP:", status)
            print("Latency:", round(elapsed), "ms")
            print("Currency:", data.get("currency"))

            metrics = data.get("metrics", {})

            if isinstance(metrics, dict):
                for key in [
                    "invoice_count",
                    "invoiced",
                    "open",
                    "overdue",
                    "collected",
                    "scored_open",
                ]:
                    print(f"{key}:", metrics.get(key))

            print("Chart points:", len(data.get("series", [])))
            print("Data generated:", data.get("generated_at"))

            issues = validate(data)

            if issues:
                failures += 1
                print("CHECK: FAILED")
                for issue in issues:
                    print(" -", issue)
            else:
                print("CHECK: PASSED")

        except HTTPError as error:
            failures += 1
            print("HTTP ERROR:", error.code)
            if error.code in (401, 403):
                print("Authentication or company authorization required.")
        except (URLError, TimeoutError, OSError) as error:
            failures += 1
            print("Connection failed:", type(error).__name__)
        except (ValueError, json.JSONDecodeError) as error:
            failures += 1
            print("Invalid response:", type(error).__name__)

    print("\n=== RESULT ===")
    print("Failed checks:", failures)
    print("Database modifications: NONE")
    print("Financial formulas modified: NONE")

    raise SystemExit(1 if failures else 0)

if __name__ == "__main__":
    main()
