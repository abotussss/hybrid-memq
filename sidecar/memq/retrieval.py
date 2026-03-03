from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .db import MemqDB
from .fact_keys import infer_query_fact_keys
from .intent import infer_intent
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
    _ = dim
    _ = bits_per_dim
    surface = search_surface(db, session_key, prompt, top_k=max(1, top_k))

    deep_called = False
    deep: List[Dict[str, Any]] = []
    q_fact_keys = infer_query_fact_keys(prompt)
    intent = infer_intent(prompt)

    if deep_enabled:
        # Call deep by coverage + intent (not only phrase matching).
        best_surface_sim = float(surface[0].get("sim", -1.0)) if surface else -1.0
        best_surface_lex = float(surface[0].get("lex", 0.0)) if surface else 0.0
        coverage_gap = (not surface) or (best_surface_sim < float(surface_threshold)) or (best_surface_lex < 0.12)
        intent_deep = bool(
            intent["profile"] >= 0.35
            or intent["timeline"] >= 0.40
            or intent["overview"] >= 0.55
            or intent["fact_lookup"] >= 0.45
            or bool(q_fact_keys)
        )
        if coverage_gap or intent_deep:
            deep_called = True
            deep = search_deep(db, session_key, prompt, top_k=max(1, top_k))

    debug = {
        "surface_count": len(surface),
        "deep_count": len(deep),
        "deep_key_hits": len([x for x in deep if int(x.get("key_overlap", 0)) > 0]),
        "deep_verified": len([x for x in deep if bool(x.get("verification_ok", True))]),
        "surface_top_score": surface[0]["score"] if surface else None,
        "surface_top_sim": surface[0]["sim"] if surface else None,
        "surface_top_lex": surface[0]["lex"] if surface else None,
        "surface_threshold": float(surface_threshold),
        "intent_profile": intent["profile"],
        "intent_timeline": intent["timeline"],
        "intent_state": intent["state"],
        "intent_fact_lookup": intent["fact_lookup"],
        "intent_meta": intent["meta"],
        "intent_overview": intent["overview"],
        "coverage_gap": 1 if ((not surface) or ((surface[0]["sim"] if surface else -1.0) < float(surface_threshold)) or ((surface[0]["lex"] if surface else 0.0) < 0.12)) else 0,
        "q_fact_keys_n": len(q_fact_keys),
    }
    return surface, deep, {"surfaceHit": bool(surface), "deepCalled": deep_called, "debug": debug}
