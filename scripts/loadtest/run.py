"""Small load test: concurrent clients hit read endpoints and report throughput and latency percentiles.

    python scripts/loadtest/run.py --base https://invoiceiq-ai.vercel.app --clients 20 --seconds 30
"""
import argparse
import asyncio
import statistics
import time

import httpx

PATHS = ["/api/portal/overview", "/api/portal/recovery-summary", "/api/portal/invoices", "/api/portal/alerts"]


async def worker(client, deadline, latencies, errors):
    i = 0
    while time.monotonic() < deadline:
        start = time.perf_counter()
        try:
            response = await client.get(PATHS[i % len(PATHS)])
            if response.status_code >= 400:
                errors.append(response.status_code)
        except httpx.HTTPError as exc:
            errors.append(type(exc).__name__)
        latencies.append((time.perf_counter() - start) * 1000)
        i += 1


async def main(base, clients, seconds):
    latencies, errors = [], []
    deadline = time.monotonic() + seconds
    async with httpx.AsyncClient(base_url=base, timeout=30, follow_redirects=True) as probe:
        await probe.post("/api/portal/showcase", headers={"Origin": base})   # the clients below share this sample session
        cookies = probe.cookies
    async with httpx.AsyncClient(base_url=base, timeout=30, cookies=cookies) as client:
        await asyncio.gather(*[worker(client, deadline, latencies, errors) for _ in range(clients)])
    latencies.sort()
    pick = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]
    from collections import Counter
    print("error breakdown:", dict(Counter(map(str, errors))) or "none")
    print(f"requests={len(latencies)} rps={len(latencies) / seconds:.1f} errors={len(errors)}")
    print(f"p50={pick(.5):.0f}ms p95={pick(.95):.0f}ms p99={pick(.99):.0f}ms mean={statistics.mean(latencies):.0f}ms")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://invoiceiq-ai.vercel.app")
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(main(args.base, args.clients, args.seconds))
