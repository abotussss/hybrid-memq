from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .db import MemqDB
from .quant import embed_text
from .retrieval_deep import search_deep
from .retrieval_surface import search_surface


def retrieve_candidates(
    *,
    db: MemqDB,
    session_key: str,
    prompt: str,
    dim: int,
    bits_per_dim: int,
    top_k: int,
    surface_threshold: float,
    deep_enabled: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    qvec = embed_text(prompt, dim)

    surface = search_surface(db, session_key, qvec, top_k=max(1, top_k), bits=bits_per_dim)

    deep_called = False
    deep: List[Dict[str, Any]] = []
    if deep_enabled:
        # Threshold semantics are cosine-sim based [-1, 1], not blended rank score.
        best_surface_sim = float(surface[0].get("sim", -1.0)) if surface else -1.0
        if not surface or best_surface_sim < float(surface_threshold):
            deep_called = True
            deep = search_deep(db, session_key, qvec, top_k=max(1, top_k), bits=bits_per_dim)

    debug = {
        "surface_count": len(surface),
        "deep_count": len(deep),
        "surface_top_score": surface[0]["score"] if surface else None,
        "surface_top_sim": surface[0]["sim"] if surface else None,
        "surface_threshold": float(surface_threshold),
    }
    return surface, deep, {"surfaceHit": bool(surface), "deepCalled": deep_called, "debug": debug}
