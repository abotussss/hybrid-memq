from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sidecar.memq.db import MemqDB, SearchResult
from sidecar.memq.brain.schemas import BrainRecallPlan


@dataclass
class RetrievalBundle:
    surface: list[SearchResult]
    deep: list[SearchResult]
    timeline: list[dict[str, Any]]
    anchors: dict[str, str]



def retrieve_with_plan(db: MemqDB, *, session_key: str, plan: BrainRecallPlan) -> RetrievalBundle:
    queries = [q for q in plan.fts_queries if str(q).strip()]
    fact_keys = [fk for fk in plan.fact_keys if str(fk).strip()]

    surface = db.search_memory(
        session_key=session_key,
        queries=queries,
        fact_keys=[],
        layers=("surface",),
        limit=max(1, plan.retrieval.topk_surface),
        include_global=False,
    ) if plan.retrieval.allow_surface else []

    deep = db.search_memory(
        session_key=session_key,
        queries=queries,
        fact_keys=fact_keys,
        layers=("deep",),
        limit=max(1, plan.retrieval.topk_deep),
        include_global=True,
    ) if plan.retrieval.allow_deep else []

    timeline: list[dict[str, Any]] = []
    if plan.retrieval.allow_timeline and plan.time_range is not None:
        timeline = db.search_events(
            session_key=session_key,
            queries=queries or [plan.time_range.label],
            start_day=plan.time_range.start_day,
            end_day=plan.time_range.end_day,
            limit=max(1, plan.retrieval.topk_events),
        )

    anchors = {
        "wm.surf": db.surface_anchor(session_key),
        "wm.deep": db.deep_anchor(session_key),
        "p.snapshot": db.profile_snapshot(session_key),
        "t.recent": db.recent_digest(session_key, days=2),
    }
    if plan.intent.profile >= 0.45 and not deep:
        fallback = db.search_memory(
            session_key=session_key,
            queries=[],
            fact_keys=["profile.identity.card"],
            layers=("deep",),
            limit=1,
            include_global=True,
        )
        deep = fallback or deep
    return RetrievalBundle(surface=surface, deep=deep, timeline=timeline, anchors=anchors)
