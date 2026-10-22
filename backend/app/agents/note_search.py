"""Hybrid note ranking: TF-IDF keywords and embedding similarity, merged by reciprocal rank fusion.

Keywords catch exact terms (an invoice number, "credit note", "voicemail"); embeddings catch paraphrases
("unhappy with the goods" finds "goods arrived damaged"). Reciprocal rank fusion merges the two rankings
without having to calibrate their scores against each other: each list contributes 1 / (K + rank). A note
counts as a semantic match only above a similarity floor, because unrelated short notes still score
around 0.7 with these embeddings; the floor is chosen on the retrieval benchmark (retrieval_eval.py).
"""
from __future__ import annotations

import re

import numpy as np

RRF_K = 60
SEMANTIC_FLOOR = 0.75  # cosine; best recall and MRR with no added false matches on notes_retrieval_v1

_STOP = frozenset("a an and are as at be by for from has have i in is it its of on or our that the their them "
                  "they this to was we were will with".split())


def _stem(word: str) -> str:
    """Just enough stemming for notes: disputes/disputed/disputing -> disput, deliveries -> deliver."""
    if word.endswith("ies") and len(word) > 5:
        word = word[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s", "e", "y"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def terms(text: str) -> list[str]:
    words = [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def lexical_scores(query: str, texts: list[str]) -> list[float]:
    """TF-IDF cosine of the query against each text; 0 where no term is shared."""
    if not texts:
        return []
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel

    vectorizer = TfidfVectorizer(analyzer=terms, sublinear_tf=True)
    try:
        matrix = vectorizer.fit_transform(texts)
        return [float(x) for x in linear_kernel(vectorizer.transform([query]), matrix).ravel()]
    except ValueError:  # nothing but stop words
        return [0.0] * len(texts)


def semantic_scores(query_vector: np.ndarray | None, vectors: list[np.ndarray | None]) -> list[float | None]:
    """Cosine of unit vectors; None where a note has no comparable embedding."""
    if query_vector is None:
        return [None] * len(vectors)
    return [None if v is None else float(np.dot(query_vector, v)) for v in vectors]


def fuse(lexical: list[float], semantic: list[float | None], floor: float = SEMANTIC_FLOOR,
         k: int = RRF_K) -> list[tuple[int, float, list[str]]]:
    """(index, fused score, matched_by) for every text that matched by keywords or by meaning, best first."""
    lex_order = [i for i in sorted(range(len(lexical)), key=lambda i: -lexical[i]) if lexical[i] > 0]
    sem_order = [i for i in sorted((i for i, s in enumerate(semantic) if s is not None), key=lambda i: -semantic[i])
                 if semantic[i] >= floor]
    scores: dict[int, float] = {}
    matched: dict[int, list[str]] = {}
    for label, order in (("keywords", lex_order), ("meaning", sem_order)):
        for rank, index in enumerate(order, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (k + rank)
            matched.setdefault(index, []).append(label)
    return sorted(((i, round(s, 5), matched[i]) for i, s in scores.items()), key=lambda x: (-x[1], x[0]))
