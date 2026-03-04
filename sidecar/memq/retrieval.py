from __future__ import annotations

import re
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
        coding_like = bool(
            re.search(
                r"(コード|code|bug|error|stack|trace|diff|patch|test|build|compile|実装|修正)",
                prompt,
                re.IGNORECASE,
            )
        )
        memory_intent = bool(
            intent["profile"] >= 0.25
            or intent["timeline"] >= 0.25
            or intent["overview"] >= 0.55
            or intent["fact_lookup"] >= 0.45
            or bool(q_fact_keys)
        )
        intent_deep = bool(
            intent["profile"] >= 0.35
            or intent["timeline"] >= 0.40
            or intent["overview"] >= 0.55
            or intent["fact_lookup"] >= 0.45
            or bool(q_fact_keys)
        )
        if coding_like and not memory_intent:
            intent_deep = False
            coverage_gap = False
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


def retrieve_candidates_with_plan(
    *,
    db: MemqDB,
    session_key: str,
    prompt: str,
    dim: int,
    bits_per_dim: int,
    top_k: int,
    surface_threshold: float,
    deep_enabled: bool,
    plan: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    _ = dim
    _ = bits_per_dim
    retrieval = plan.get("retrieval") if isinstance(plan, dict) else {}
    fact_keys = set(infer_query_fact_keys(prompt))
    if isinstance(plan, dict):
        fact_keys.update(str(k).strip() for k in (plan.get("fact_keys") or []) if str(k).strip())
    queries = [str(q).strip() for q in (plan.get("fts_queries") or []) if str(q).strip()] if isinstance(plan, dict) else []
    if not queries:
        queries = [prompt]
    queries = queries[:4]

    topk_surface = max(1, min(50, int(retrieval.get("topk_surface", top_k))))
    topk_deep = max(1, min(50, int(retrieval.get("topk_deep", top_k))))
    allow_deep = bool(retrieval.get("allow_deep", deep_enabled)) and bool(deep_enabled)

    surface_by_id: Dict[str, Dict[str, Any]] = {}
    deep_by_id: Dict[str, Dict[str, Any]] = {}

    for q in queries:
        for row in search_surface(db, session_key, q, top_k=topk_surface):
            rid = str(row.get("id"))
            cand = dict(row)
            if fact_keys:
                tag_keys = set(cand.get("tag_keys") or [])
                row_keys = set(cand.get("fact_keys") or [])
                overlap = len(fact_keys & (tag_keys | row_keys))
                if overlap > 0:
                    cand["score"] = float(cand.get("score", 0.0)) + min(0.36, 0.14 * overlap)
                    cand["key_overlap"] = overlap
            prev = surface_by_id.get(rid)
            if prev is None or float(cand.get("score", 0.0)) > float(prev.get("score", 0.0)):
                surface_by_id[rid] = cand
        if allow_deep:
            for row in search_deep(db, session_key, q, top_k=topk_deep):
                rid = str(row.get("id"))
                cand = dict(row)
                if fact_keys:
                    tag_keys = set(cand.get("tag_keys") or [])
                    row_keys = set(cand.get("fact_keys") or [])
                    overlap = len(fact_keys & (tag_keys | row_keys))
                    if overlap > 0:
                        cand["score"] = float(cand.get("score", 0.0)) + min(0.42, 0.16 * overlap)
                prev = deep_by_id.get(rid)
                if prev is None or float(cand.get("score", 0.0)) > float(prev.get("score", 0.0)):
                    deep_by_id[rid] = cand

    surface = sorted(surface_by_id.values(), key=lambda x: float(x.get("score", 0.0)), reverse=True)[:max(1, top_k)]
    deep = sorted(deep_by_id.values(), key=lambda x: float(x.get("score", 0.0)), reverse=True)[:max(1, top_k)]

    best_surface_sim = float(surface[0].get("sim", -1.0)) if surface else -1.0
    best_surface_lex = float(surface[0].get("lex", 0.0)) if surface else 0.0
    coverage_gap = (not surface) or (best_surface_sim < float(surface_threshold)) or (best_surface_lex < 0.12)
    debug = {
        "surface_count": len(surface),
        "deep_count": len(deep),
        "surface_top_score": surface[0]["score"] if surface else None,
        "surface_top_sim": surface[0]["sim"] if surface else None,
        "surface_top_lex": surface[0]["lex"] if surface else None,
        "surface_threshold": float(surface_threshold),
        "coverage_gap": 1 if coverage_gap else 0,
        "brain_plan_used": 1,
        "brain_plan_queries_n": len(queries),
        "brain_plan_fact_keys_n": len(fact_keys),
        "brain_plan_allow_deep": 1 if allow_deep else 0,
    }
    return surface, deep, {"surfaceHit": bool(surface), "deepCalled": bool(allow_deep), "debug": debug}
