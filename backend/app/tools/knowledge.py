"""Bounded local vector retrieval over explicitly allowed product evidence.

TF-IDF embeddings are lexical, not a remote semantic embedding model. Nothing
is uploaded and no arbitrary filesystem path or private document is accepted.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from app.agents.guardrails import is_untrusted_instruction

BACKEND = Path(__file__).resolve().parents[2]
SOURCES = {
    "feature-provenance": BACKEND / "docs/FEATURE_PROVENANCE.md",
    "evaluation-protocol": BACKEND / "EVALUATION_PROTOCOL.md",
    "serving-calibration": BACKEND / "app/ml/artifacts/b2b_v2/calibration/serving_calibration_policy.json",
    "recovery-policy": BACKEND / "app/services/policy_engine.py",
}


@dataclass(frozen=True)
class Evidence:
    source_id: str
    source_sha256: str
    chunk_id: str
    start_line: int
    end_line: int
    excerpt: str
    relevance: float = 0


class KnowledgeIndex:
    def __init__(self, documents: dict[str, str]):
        self.chunks: list[Evidence] = []
        for source, content in documents.items():
            if source not in SOURCES or len(content) > 200_000:
                raise ValueError("Source not allowed or exceeds ingestion limit")
            digest = sha256(content.encode()).hexdigest()
            lines = content.splitlines()
            start = 0
            while start < len(lines):
                end = start
                size = 0
                while end < len(lines) and size < 1200:
                    size += len(lines[end]) + 1
                    end += 1
                excerpt = "\n".join(lines[start:end])[:2400]
                if excerpt.strip():
                    self.chunks.append(Evidence(source, digest, f"{source}:{start + 1}-{end}",
                                                start + 1, end, excerpt))
                start = max(start + 1, end - 3)
        self.vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=12000)
        self.vectors = self.vectorizer.fit_transform([item.excerpt for item in self.chunks]) if self.chunks else None

    def retrieve(self, query: str, *, allowed_sources: set[str], limit: int = 3) -> list[dict]:
        if not allowed_sources.issubset(SOURCES) or not 1 <= limit <= 5:
            raise ValueError("Invalid retrieval scope or limit")
        if len(query) > 1000 or is_untrusted_instruction(query) or self.vectors is None:
            return []
        scores = (self.vectors @ self.vectorizer.transform([query]).T).toarray().ravel()
        ranked = np.argsort(-scores, kind="stable")
        results = []
        for index in ranked:
            chunk = self.chunks[int(index)]
            if chunk.source_id in allowed_sources and scores[index] > 0:
                results.append({**asdict(chunk), "relevance": float(scores[index])})
                if len(results) == limit:
                    break
        return results


def retrieve_investigation_evidence(query: str) -> list[dict]:
    # Read the current source bytes, so evidence fingerprints never reference a stale cache.
    documents = {source: path.read_text() for source, path in SOURCES.items() if path.is_file()}
    return KnowledgeIndex(documents).retrieve(query, allowed_sources=set(SOURCES))
