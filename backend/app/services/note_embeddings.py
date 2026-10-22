"""Embeddings for invoice notes, used by the hybrid (keyword + semantic) note search.

Notes are embedded once when written (and backfilled for older notes); a question is embedded when the
assistant searches. Vectors are unit length, so cosine similarity is a dot product, and stored as float32
bytes beside the note with the model that produced them: a vector from another model is never compared.
Any failure (no key, quota, timeout) returns None and the search quietly stays keyword-only.
"""
from __future__ import annotations

import asyncio
import logging

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)
DIMENSIONS = 256
DEFAULT_MODEL = "gemini-embedding-001"


def model_name() -> str | None:
    name = (settings.note_embedding_model or DEFAULT_MODEL).removeprefix("models/")
    return None if name.lower() == "none" else name


def available() -> bool:
    return bool(settings.google_api_key and model_name())


def pack(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack(blob: bytes | None) -> np.ndarray | None:
    if not blob or len(blob) != DIMENSIONS * 4:  # missing, or not a vector this code wrote
        return None
    vector = np.frombuffer(blob, dtype=np.float32)
    return vector if vector.size == DIMENSIONS else None


def _unit(vector) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return v / norm if norm else v


async def embed(texts: list[str], task: str, *, model: str | None = None, api_key: str | None = None,
                timeout: float = 10.0) -> list[np.ndarray] | None:
    """Unit vectors for texts; task is "document" (notes) or "query" (a search). None on any failure."""
    model = model or model_name()
    api_key = api_key or settings.google_api_key
    if not (model and api_key and texts):
        return None
    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        client = GoogleGenerativeAIEmbeddings(model=f"models/{model}", google_api_key=api_key)
        vectors = await asyncio.wait_for(client.aembed_documents(
            [t[:2000] for t in texts], task_type="RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT",
            output_dimensionality=DIMENSIONS), timeout=timeout)
        return [_unit(v) for v in vectors]
    except Exception:  # noqa: BLE001 - search falls back to keywords
        logger.warning("Note embedding failed; note search stays keyword-only", exc_info=True)
        return None


async def embed_query(text: str) -> np.ndarray | None:
    vectors = await embed([text], "query", timeout=6.0)
    return vectors[0] if vectors else None


async def backfill(session, workspace_id=None, limit: int = 500) -> int:
    """Embed notes that have no vector from the current model; returns how many were embedded."""
    from sqlalchemy import or_, select

    from app.models.portal import PortalNote

    model = model_name()
    if not available():
        return 0
    filters = [or_(PortalNote.embedding.is_(None), PortalNote.embedding_model != model)]
    if workspace_id is not None:
        filters.append(PortalNote.workspace_id == workspace_id)
    notes = (await session.scalars(select(PortalNote).where(*filters).limit(limit))).all()
    done = 0
    for start in range(0, len(notes), 100):
        batch = notes[start:start + 100]
        vectors = await embed([n.body for n in batch], "document", timeout=30.0)
        if vectors is None:
            break
        for note, vector in zip(batch, vectors):
            note.embedding, note.embedding_model = pack(vector), model
        done += len(batch)
    await session.flush()
    return done
