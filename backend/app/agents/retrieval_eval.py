"""Retrieval benchmark for note search: keyword-only vs meaning-only vs hybrid, on hand-labelled queries.

    python -m app.agents.retrieval_eval --embed gemini-embedding-001   # fetch and cache embeddings (needs a key)
    python -m app.agents.retrieval_eval                                # offline, from the cache
    python -m app.agents.retrieval_eval --sweep                        # also choose the semantic floor

Metrics per query, averaged: recall@3 (share of relevant notes in the top 3), MRR (1 / rank of the first
relevant note) and hit@1. Embeddings are cached in tests/fixtures/notes_retrieval_embeddings.json so CI runs
this offline and deterministically.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

from app.agents.note_search import SEMANTIC_FLOOR, fuse, lexical_scores, semantic_scores

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
BENCHMARK = FIXTURES / "notes_retrieval_v1.json"
CACHE = FIXTURES / "notes_retrieval_embeddings.json"
METHODS = ("keywords", "meaning", "hybrid")


def load() -> tuple[dict, list[dict]]:
    data = json.loads(BENCHMARK.read_text())
    return data["notes"], data["queries"]


def load_cache() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


async def embed_and_cache(model: str) -> None:
    from app.services.note_embeddings import embed

    notes, queries = load()
    note_ids = list(notes)
    note_vectors = await embed([notes[i] for i in note_ids], "document", model=model, timeout=60)
    query_vectors = await embed([q["text"] for q in queries], "query", model=model, timeout=60)
    if note_vectors is None or query_vectors is None:
        raise SystemExit(f"Could not embed with {model}; check GOOGLE_API_KEY and quota")
    cache = load_cache()
    cache[model] = {"notes": {i: [round(float(x), 5) for x in v] for i, v in zip(note_ids, note_vectors)},
                    "queries": {q["id"]: [round(float(x), 5) for x in v] for q, v in zip(queries, query_vectors)}}
    CACHE.write_text(json.dumps(cache, separators=(",", ":")) + "\n")
    print(f"cached {len(note_ids)} note and {len(queries)} query embeddings for {model}")


def rank(method: str, query: dict, notes: dict, vectors: dict | None, floor: float) -> list[str]:
    ids = list(notes)
    lexical = lexical_scores(query["text"], [notes[i] for i in ids]) if method != "meaning" else [0.0] * len(ids)
    semantic = [None] * len(ids)
    if method != "keywords" and vectors:
        q = np.asarray(vectors["queries"][query["id"]], dtype=np.float32)
        semantic = semantic_scores(q / np.linalg.norm(q), [
            np.asarray(vectors["notes"][i], dtype=np.float32) / np.linalg.norm(vectors["notes"][i]) for i in ids])
    return [ids[i] for i, _, _ in fuse(lexical, semantic, floor=floor)]


def score(ranked: list[str], relevant: list[str]) -> dict:
    first = next((n for n, i in enumerate(ranked, start=1) if i in relevant), None)
    return {"recall@3": len(set(ranked[:3]) & set(relevant)) / min(3, len(relevant)),
            "mrr": 1 / first if first else 0.0, "hit@1": float(bool(ranked) and ranked[0] in relevant)}


def evaluate(model: str | None, floor: float = SEMANTIC_FLOOR) -> dict:
    notes, queries = load()
    vectors = load_cache().get(model) if model else None
    report = {}
    for method in METHODS:
        if method != "keywords" and vectors is None:
            continue
        answerable = [q for q in queries if q["relevant"]]
        rows = [{"type": q["type"], **score(rank(method, q, notes, vectors, floor), q["relevant"])} for q in answerable]
        groups = {"all": rows, **{t: [r for r in rows if r["type"] == t] for t in ("keyword", "mixed", "paraphrase")}}
        report[method] = {g: {m: round(sum(r[m] for r in rs) / len(rs), 3) for m in ("recall@3", "mrr", "hit@1")}
                          for g, rs in groups.items() if rs}
        unrelated = [q for q in queries if not q["relevant"]]
        report[method]["false_matches"] = round(
            sum(len(rank(method, q, notes, vectors, floor)) for q in unrelated) / max(1, len(unrelated)), 2)
    return report


def table(report: dict) -> str:
    lines = ["| Method | Recall@3 | MRR | Hit@1 | Recall@3: keyword / mixed / paraphrase queries | "
             "False matches per unrelated query |", "|---|---|---|---|---|---|"]
    for method, groups in report.items():
        a = groups["all"]
        lines.append(f"| {method} | {a['recall@3']:.2f} | {a['mrr']:.2f} | {a['hit@1']:.2f} | "
                     f"{groups['keyword']['recall@3']:.2f} / {groups['mixed']['recall@3']:.2f} / "
                     f"{groups['paraphrase']['recall@3']:.2f} | {groups['false_matches']:.1f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--embed", metavar="MODEL", help="fetch and cache embeddings for this Gemini model")
    parser.add_argument("--model", default=None, help="cached model to evaluate (default: the configured one)")
    parser.add_argument("--sweep", action="store_true", help="report hybrid quality across semantic floors")
    args = parser.parse_args(argv)
    if args.embed:
        asyncio.run(embed_and_cache(args.embed))
        return 0
    from app.services.note_embeddings import DEFAULT_MODEL

    cache = load_cache()
    models = [args.model] if args.model else (sorted(cache) or [None])
    for model in models:
        print(f"\n{model or 'no embeddings cached'} (semantic floor {SEMANTIC_FLOOR})")
        print(table(evaluate(model)))
        if args.sweep and model in cache:
            for floor in (0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.84):
                hybrid = evaluate(model, floor)["hybrid"]
                h = hybrid["all"]
                print(f"  floor {floor:.2f}: recall@3 {h['recall@3']:.3f}  mrr {h['mrr']:.3f}  hit@1 {h['hit@1']:.3f}  "
                      f"false matches {hybrid['false_matches']:.1f}")
    if DEFAULT_MODEL not in cache:
        print(f"\nNo cached embeddings for {DEFAULT_MODEL}; run with --embed {DEFAULT_MODEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
