"""Check every model in the assistant's fallback chain with one real tool call.

    python -m app.agents.check_providers          # test the configured chain
    python -m app.agents.check_providers --groq   # also list the models your Groq key can use

Each call spends one request of that provider's free quota. Keys are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from time import perf_counter

from app.agents.finance_agent import build_providers, classify_failure
from app.agents.finance_tools import TOOL_SPECS


async def probe(provider) -> str:
    started = perf_counter()
    try:
        reply = await asyncio.wait_for(provider.bound().ainvoke([("human", "How much is overdue?")]), timeout=30)
    except asyncio.TimeoutError:
        return "timeout after 30 s"
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        failure = classify_failure(exc)
        if failure.kind == "rate_limited":
            return f"rate limited, retry in {failure.retry_after:.0f} s"
        return "model not found" if failure.kind == "not_found" else f"error: {type(exc).__name__}"
    tools = [c["name"] for c in getattr(reply, "tool_calls", [])]
    verdict = "OK" if tools else "answered without calling a tool (weak tool use)"
    return f"{verdict} in {perf_counter() - started:.1f} s; tools: {', '.join(tools) or 'none'}"


def groq_models() -> list[str]:
    import httpx

    from app.core.config import settings

    if not settings.groq_api_key:
        return []
    response = httpx.get("https://api.groq.com/openai/v1/models", timeout=20,
                         headers={"Authorization": f"Bearer {settings.groq_api_key}"})
    response.raise_for_status()
    return sorted(m["id"] for m in response.json().get("data", []) if m.get("active", True))


async def main_async(list_groq: bool) -> int:
    providers = build_providers()
    if not providers:
        print("No model configured: set GOOGLE_API_KEY, or GROQ_API_KEY with GROQ_MODEL.")
        return 1
    print(f"Fallback chain ({len(TOOL_SPECS)} tools offered):")
    working = 0
    for i, provider in enumerate(providers, 1):
        result = await probe(provider)
        working += result.startswith("OK")
        print(f"  {i}. {provider.name:<7} {provider.model:<32} {result}")
    if not any(p.name == "groq" for p in providers):
        print("  Groq is not in the chain: set GROQ_API_KEY and GROQ_MODEL to add it.")
    if list_groq:
        models = groq_models()
        print("Groq models for this key:" if models else "No Groq key set.")
        for model in models:
            print("  ", model)
    print("Then the rule-based answer, which always works.")
    return 0 if working else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--groq", action="store_true", help="list the models your Groq key can use")
    return asyncio.run(main_async(parser.parse_args(argv).groq))


if __name__ == "__main__":
    sys.exit(main())
