from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .db import MemqDB
from .fact_keys import infer_query_fact_keys
from .quant import embed_text
from .retrieval_deep import search_deep
from .retrieval_surface import search_surface


def _embed_query_text(prompt: str) -> str:
    q = prompt or ""
    keys: List[str] = sorted(list(infer_query_fact_keys(q)))
    if not keys:
        return q
    return f"{q}\n[memq_keys={','.join(keys)}]"


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
    qvec = embed_text(_embed_query_text(prompt), dim)

    surface = search_surface(db, session_key, prompt, qvec, top_k=max(1, top_k), bits=bits_per_dim)

    deep_called = False
    deep: List[Dict[str, Any]] = []
    long_term_required = bool(
        re.search(
            r"(覚えてる|記憶|これまで|長期|long[-\s]?term|family|家族|人格|persona|呼称|一人称|10分前|直近|recent)",
            prompt or "",
            re.IGNORECASE,
        )
    )
    if deep_enabled:
        # Call deep when surface confidence or lexical coverage is insufficient.
        best_surface_sim = float(surface[0].get("sim", -1.0)) if surface else -1.0
        best_surface_lex = float(surface[0].get("lex", 0.0)) if surface else 0.0
        min_lex = 0.12
        if long_term_required or (not surface) or (best_surface_sim < float(surface_threshold)) or (best_surface_lex < min_lex):
            deep_called = True
            deep = search_deep(db, session_key, prompt, qvec, top_k=max(1, top_k), bits=bits_per_dim)

    debug = {
        "surface_count": len(surface),
        "deep_count": len(deep),
        "deep_key_hits": len([x for x in deep if int(x.get("key_overlap", 0)) > 0]),
        "deep_verified": len([x for x in deep if bool(x.get("verification_ok", True))]),
        "surface_top_score": surface[0]["score"] if surface else None,
        "surface_top_sim": surface[0]["sim"] if surface else None,
        "surface_top_lex": surface[0]["lex"] if surface else None,
        "surface_threshold": float(surface_threshold),
        "long_term_required": long_term_required,
    }
    return surface, deep, {"surfaceHit": bool(surface), "deepCalled": deep_called, "debug": debug}
