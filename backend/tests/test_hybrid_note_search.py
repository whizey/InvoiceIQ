"""Hybrid note search: keyword and semantic rankings fused, measured on the hand-labelled benchmark."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from app.agents.assistant_eval import load_fixture
from app.agents.finance_tools import run_tool
from app.agents.note_search import SEMANTIC_FLOOR, fuse
from app.agents.retrieval_eval import evaluate
from app.services import note_embeddings


def unit(*values):
    v = np.zeros(note_embeddings.DIMENSIONS, dtype=np.float32)
    v[: len(values)] = values
    return v / np.linalg.norm(v)


def test_fusion_merges_both_rankings_and_respects_the_floor():
    lexical = [0.9, 0.0, 0.2, 0.0]
    semantic = [0.70, 0.95, None, 0.76]
    ranked = fuse(lexical, semantic, floor=0.75)
    # Each list gives 1/(60+rank): notes 0 and 1 tie at 1/61, notes 2 and 3 at 1/62; ties keep note order.
    assert [i for i, _, _ in ranked] == [0, 1, 2, 3]
    how = {i: matched for i, _, matched in ranked}
    assert how[0] == ["keywords"] and how[1] == ["meaning"] and how[3] == ["meaning"] and how[2] == ["keywords"]
    both = fuse([0.5, 0.4], [0.9, 0.8], floor=0.75)
    assert both[0][0] == 0 and both[0][2] == ["keywords", "meaning"]
    assert fuse([0.0, 0.0], [0.5, 0.6], floor=0.75) == []  # below the floor and no shared words: no match


@pytest.fixture
def portfolio_with_vectors():
    """The eval fixture's notes, given toy embeddings: 'damage' and 'unhappy' point the same way."""
    p = load_fixture()[0]
    directions = {"INV-1001": unit(1, 0, 0), "INV-1002": unit(0, 1, 0), "INV-1009": unit(0, 0, 1),
                  "INV-1003": unit(0.2, 0, 0.9), "INV-1005": unit(0, 0.3, 0.9), "INV-1007": unit(0, 0, 0, 1)}
    by_id = {r.id: r.invoice_number for r in p.invoices}
    for note in p.notes:
        note.embedding = note_embeddings.pack(directions[by_id[note.invoice_id]])
        note.embedding_model = note_embeddings.model_name()
    calls = []

    async def embed_query(text):
        calls.append(text)
        if "credit" in text:
            return unit(0.2, 0, 0.9)  # near INV-1003's credit-note dispute
        return unit(0.97, 0, 0.1)  # near INV-1001's delivery dispute
    p.embed_query = embed_query
    p.embed_calls = calls
    return p


def test_a_paraphrase_finds_a_note_with_no_shared_words(portfolio_with_vectors):
    found = run_tool(portfolio_with_vectors, "search_notes", {"query": "client upset about what arrived"})
    assert found["search"] == "hybrid" and found["notes"][0]["invoice"] == "INV-1001"
    assert found["notes"][0]["matched_by"] == ["meaning"]
    assert portfolio_with_vectors.embed_calls == ["client upset about what arrived"]


def test_keywords_still_rank_exact_terms(portfolio_with_vectors):
    found = run_tool(portfolio_with_vectors, "search_notes", {"query": "credit note pending"})
    assert found["notes"][0]["invoice"] == "INV-1003" and "keywords" in found["notes"][0]["matched_by"]


def test_keyword_only_without_embeddings(portfolio_with_vectors):
    p = portfolio_with_vectors
    for note in p.notes:
        note.embedding_model = "some-other-model"  # vectors from another model are never compared
    found = run_tool(p, "search_notes", {"query": "client upset about what arrived"})
    assert found["search"] == "keywords" and p.embed_calls == []  # nothing to compare, so no embedding call
    p.embed_query = None
    assert run_tool(p, "search_notes", {"query": "dispute", "kind": "dispute"})["search"] == "keywords"


def test_hybrid_beats_keywords_on_the_benchmark_without_extra_false_matches():
    report = evaluate(note_embeddings.DEFAULT_MODEL, SEMANTIC_FLOOR)
    keywords, hybrid = report["keywords"], report["hybrid"]
    assert hybrid["all"]["recall@3"] > keywords["all"]["recall@3"]
    assert hybrid["all"]["mrr"] > keywords["all"]["mrr"]
    assert hybrid["paraphrase"]["recall@3"] > keywords["paraphrase"]["recall@3"]
    assert hybrid["keyword"]["recall@3"] >= keywords["keyword"]["recall@3"]  # exact terms not lost
    assert hybrid["false_matches"] <= keywords["false_matches"]


def test_backfill_embeds_notes_without_a_current_vector(monkeypatch):
    stored = [SimpleNamespace(body="Goods arrived damaged", embedding=None, embedding_model=None),
              SimpleNamespace(body="Promised to pay", embedding=b"old", embedding_model="old-model")]

    class Session:
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: stored)

        async def flush(self):
            pass

    async def fake_embed(texts, task, **kwargs):
        assert task == "document"
        return [unit(1, 0) for _ in texts]

    monkeypatch.setattr(note_embeddings, "available", lambda: True)
    monkeypatch.setattr(note_embeddings, "embed", fake_embed)
    assert asyncio.run(note_embeddings.backfill(Session())) == 2
    assert all(n.embedding_model == note_embeddings.model_name() for n in stored)
    assert np.allclose(note_embeddings.unpack(stored[0].embedding), unit(1, 0))


def test_embedding_failures_return_none(monkeypatch):
    monkeypatch.setattr(note_embeddings.settings, "google_api_key", None)
    assert asyncio.run(note_embeddings.embed(["text"], "document")) is None
    assert note_embeddings.unpack(b"short") is None
