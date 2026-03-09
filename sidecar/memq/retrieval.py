from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import re
import time
from typing import Any

from sidecar.memq.db import MemqDB, SearchResult
from sidecar.memq.brain.schemas import BrainRecallPlan
from sidecar.memq.lancedb_bridge import LanceDbMemoryBackend
from sidecar.memq.memory_source import deep_anchor, qctx_profile_snapshot, surface_anchor


@dataclass
class RetrievalBundle:
    surface: list[SearchResult]
    deep: list[SearchResult]
    timeline: list[dict[str, Any]]
    anchors: dict[str, str]
    debug: dict[str, Any] = field(default_factory=dict)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff_-]+", str(text or "").lower()))


def _lexical_overlap(queries: list[str], text: str) -> float:
    query_tokens = _tokens(" ".join(queries))
    if not query_tokens:
        return 0.0
    text_tokens = _tokens(text)
    if not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    return min(1.0, overlap / max(1, min(len(query_tokens), 4)))


def _resolve_limit(requested: int, top_k: int | None) -> int:
    if top_k is None or top_k <= 0:
        return max(1, requested)
    return max(1, top_k)


def _query_text(plan: BrainRecallPlan) -> str:
    return " ".join([*plan.fts_queries, *plan.fact_keys])


def _query_complexity(plan: BrainRecallPlan) -> int:
    return len(_tokens(_query_text(plan)))


def _semantic_overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _result_text(item: SearchResult) -> str:
    return " ".join(
        part
        for part in (
            str(item.fact_key or ""),
            str(item.value or ""),
            str(getattr(item, "text", "") or ""),
            str(item.summary or ""),
        )
        if part
    )


def _recency_bonus(updated_at: int) -> float:
    if updated_at <= 0:
        return 0.0
    age_days = max(0.0, (max(0.0, time.time() - updated_at)) / 86400.0)
    return 0.22 / (1.0 + age_days / 7.0)


def _adaptive_limits(plan: BrainRecallPlan, top_k: int | None) -> dict[str, int]:
    limits = {
        "surface": _resolve_limit(plan.retrieval.topk_surface, top_k),
        "deep": _resolve_limit(plan.retrieval.topk_deep, top_k),
        "events": _resolve_limit(plan.retrieval.topk_events, top_k),
    }
    complexity = _query_complexity(plan)
    if complexity >= 6:
        limits["surface"] += 1
        limits["deep"] += 1
    if float(plan.intent.timeline) >= 0.55:
        limits["events"] += 2
        limits["deep"] = max(1, limits["deep"] - 1)
    if float(plan.intent.profile) >= 0.55:
        limits["deep"] += 1
        limits["surface"] = max(1, limits["surface"] - 1)
    if float(plan.intent.state + plan.intent.overview) >= 0.55:
        limits["surface"] += 2
    if top_k is not None and top_k > 0:
        for key in limits:
            limits[key] = min(limits[key], top_k)
    return limits


def _memory_noise(item: SearchResult) -> bool:
    payload = _result_text(item).lower()
    if not payload.strip():
        return True
    if "budget_tokens=" in payload or "<mem" in payload:
        return True
    if len(_tokens(payload)) < 2 and not str(item.fact_key or "").strip():
        return True
    return False


def _memory_allowed(item: SearchResult, plan: BrainRecallPlan) -> bool:
    if _memory_noise(item):
        return False
    payload = _result_text(item)
    overlap = _lexical_overlap(plan.fts_queries + plan.fact_keys, payload)
    fact_match = bool(item.fact_key) and item.fact_key in plan.fact_keys
    fact_key = str(item.fact_key or "").lower()
    if fact_key.startswith("qstyle.") or fact_key.startswith("qrule."):
        return False

    if float(plan.intent.timeline) >= 0.6 and fact_key.startswith("profile.") and not fact_match and overlap < 0.18:
        return False
    if float(plan.intent.profile) >= 0.55 and plan.fact_keys:
        identity_fact = fact_key in {
            "profile.identity.card",
            "profile.name",
            "profile.display_name",
            "profile.alias",
            "profile.nickname",
            "profile.user_name",
        }
        if fact_key.startswith("profile.") and not (fact_match or identity_fact or overlap >= 0.18):
            return False
    if float(plan.intent.fact) >= 0.55 and plan.fact_keys and not fact_match and overlap < 0.14:
        if not fact_key.startswith("project.") and not fact_key.startswith("timeline."):
            return False
    if (item.score + overlap + float(item.importance or 0.0)) < 0.25:
        return False
    return True


def _diversify_results(items: list[SearchResult], limit: int) -> list[SearchResult]:
    if len(items) <= limit:
        return items[:limit]
    remaining = list(items)
    selected: list[SearchResult] = []
    while remaining and len(selected) < max(1, limit):
        best_index = 0
        best_score = float("-inf")
        for index, candidate in enumerate(remaining):
            redundancy = 0.0
            if selected:
                redundancy = max(_semantic_overlap(_result_text(candidate), _result_text(existing)) for existing in selected)
            utility = float(candidate.score) - redundancy * 0.35
            if utility > best_score:
                best_score = utility
                best_index = index
        selected.append(remaining.pop(best_index))
    return selected


def _diversify_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(events) <= limit:
        return events[:limit]
    remaining = list(events)
    selected: list[dict[str, Any]] = []
    while remaining and len(selected) < max(1, limit):
        best_index = 0
        best_score = float("-inf")
        for index, candidate in enumerate(remaining):
            cand_text = str(candidate.get("text") or candidate.get("summary") or "")
            redundancy = 0.0
            if selected:
                redundancy = max(
                    _semantic_overlap(cand_text, str(existing.get("text") or existing.get("summary") or ""))
                    for existing in selected
                )
            utility = float(candidate.get("_rank_score") or 0.0) - redundancy * 0.30
            if utility > best_score:
                best_score = utility
                best_index = index
        selected.append(remaining.pop(best_index))
    return selected


def _memory_intent_bonus(item: SearchResult, plan: BrainRecallPlan, *, preferred_layer: str) -> float:
    fact_key = str(item.fact_key or "").lower()
    payload = f"{item.fact_key} {item.value} {item.summary}"
    state_intent = max(float(plan.intent.state), float(plan.intent.overview))
    bonus = _lexical_overlap(plan.fts_queries, payload) * 0.35
    bonus += float(item.importance or 0.0) * 0.10
    bonus += float(item.strength or 0.0) * 0.12
    bonus += _recency_bonus(int(item.updated_at or 0))

    if item.session_key != "global":
        bonus += 0.18
    elif not fact_key.startswith("profile."):
        bonus -= 0.08

    if item.layer == preferred_layer:
        bonus += 0.16

    if float(plan.intent.profile) > 0.0:
        profile_overlap = _lexical_overlap(plan.fts_queries + plan.fact_keys, payload)
        if fact_key.startswith("profile."):
            if item.fact_key in plan.fact_keys:
                bonus += float(plan.intent.profile) * 0.95
            elif fact_key in {
                "profile.identity.card",
                "profile.name",
                "profile.display_name",
                "profile.alias",
                "profile.nickname",
                "profile.user_name",
            }:
                bonus += float(plan.intent.profile) * 0.55
            else:
                bonus += float(plan.intent.profile) * profile_overlap * 0.75
                if profile_overlap < 0.12 and plan.fact_keys:
                    bonus -= float(plan.intent.profile) * 0.20
        elif preferred_layer == "deep":
            bonus -= float(plan.intent.profile) * 0.12

    if float(plan.intent.timeline) > 0.0:
        if fact_key.startswith("timeline."):
            bonus += float(plan.intent.timeline) * 0.95
        elif item.layer == "surface" and "recent" in payload.lower():
            bonus += float(plan.intent.timeline) * 0.15

    if state_intent > 0.0:
        if item.layer == "surface":
            bonus += state_intent * 0.70
        elif fact_key.startswith("project.") or fact_key.startswith("state."):
            bonus += state_intent * 0.35

    if float(plan.intent.fact) > 0.0 and item.layer == "deep":
        if not fact_key.startswith("profile.") and not fact_key.startswith("timeline."):
            bonus += float(plan.intent.fact) * 0.60

    if plan.fact_keys and item.fact_key in plan.fact_keys:
        bonus += 0.45

    return bonus


def _rerank_memory_results(items: list[SearchResult], plan: BrainRecallPlan, *, preferred_layer: str, limit: int) -> list[SearchResult]:
    reranked = [
        replace(item, score=item.score + _memory_intent_bonus(item, plan, preferred_layer=preferred_layer))
        for item in items
        if _memory_allowed(item, plan)
    ]
    ranked = sorted(reranked, key=lambda item: (item.score, item.updated_at), reverse=True)
    return _diversify_results(ranked, max(1, limit))


def _event_intent_bonus(event: dict[str, Any], plan: BrainRecallPlan) -> float:
    summary = str(event.get("text") or event.get("summary") or "")
    score = float(event.get("salience") or 0.0) * 0.60
    score += _lexical_overlap(plan.fts_queries, summary) * 0.50
    if plan.time_range is not None and str(event.get("day_key") or "") in {plan.time_range.start_day, plan.time_range.end_day}:
        score += 0.15
    return score


def _rerank_events(events: list[dict[str, Any]], plan: BrainRecallPlan, *, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        events,
        key=lambda event: (
            _event_intent_bonus(event, plan),
            float(event.get("salience") or 0.0),
            int(event.get("ts") or 0),
        ),
        reverse=True,
    )
    for event in ranked:
        event["_rank_score"] = _event_intent_bonus(event, plan)
    return _diversify_events(ranked, max(1, limit))


def _dict_to_search_result(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        id=int(item.get("numeric_id") or 0),
        session_key=str(item.get("session_key") or ""),
        layer=str(item.get("layer") or ""),
        kind=str(item.get("kind") or ""),
        text=str(item.get("text") or ""),
        fact_key=str(item.get("fact_key") or ""),
        value=str(item.get("value") or ""),
        summary=str(item.get("summary") or ""),
        confidence=float(item.get("confidence") or 0.0),
        importance=float(item.get("importance") or 0.0),
        strength=float(item.get("strength") or 0.0),
        updated_at=int(item.get("timestamp") or 0),
        score=float(item.get("score") or 0.0),
    )


def _search_memory(
    db: MemqDB,
    memory_backend: LanceDbMemoryBackend | None,
    *,
    session_key: str,
    queries: list[str],
    fact_keys: list[str],
    layer: str,
    limit: int,
    kinds: list[str] | None,
    include_global: bool,
) -> list[SearchResult]:
    if memory_backend is not None and memory_backend.enabled():
        rows = memory_backend.search_memories(
            session_key=session_key,
            queries=queries,
            fact_keys=fact_keys,
            layer=layer,
            limit=limit,
            kinds=kinds,
            include_global=include_global,
        )
        return [_dict_to_search_result(row) for row in rows]
    return db.search_memory(
        session_key=session_key,
        queries=queries,
        fact_keys=fact_keys,
        layers=(layer,),
        limit=limit,
        include_global=include_global,
    )


def _search_timeline(
    db: MemqDB,
    memory_backend: LanceDbMemoryBackend | None,
    *,
    session_key: str,
    queries: list[str],
    start_day: str,
    end_day: str,
    limit: int,
) -> list[dict[str, Any]]:
    if memory_backend is not None and memory_backend.enabled():
        rows = memory_backend.list_entries(
            session_key=session_key,
            kinds=["digest", "event"],
            include_global=False,
            limit=max(12, limit * 6),
        )
        event_rows: list[dict[str, Any]] = []
        digest_rows: list[dict[str, Any]] = []
        for row in rows:
            ts = int(row.get("timestamp") or 0)
            if not ts:
                continue
            day_key = datetime.fromtimestamp(ts, tz=db.timezone).strftime("%Y-%m-%d")
            if day_key < start_day or day_key > end_day:
                continue
            raw_text = str(row.get("text") or "").strip()
            summary = str(row.get("summary") or raw_text).strip()
            if not raw_text and not summary:
                continue
            entry = (
                {
                    "id": row.get("id"),
                    "text": raw_text,
                    "summary": summary,
                    "ts": ts,
                    "day_key": day_key,
                    "actor": "memory",
                    "kind": str(row.get("kind") or "event"),
                    "salience": float(row.get("importance") or row.get("strength") or 0.5),
                }
            )
            if str(row.get("kind") or "event") == "event":
                event_rows.append(entry)
            else:
                digest_rows.append(entry)
        out = event_rows if event_rows else digest_rows
        if out:
            return out[: max(1, limit * 4)]
        return []
    return db.search_events(
        session_key=session_key,
        queries=queries,
        start_day=start_day,
        end_day=end_day,
        limit=limit,
    )



def retrieve_with_plan(
    db: MemqDB,
    *,
    session_key: str,
    plan: BrainRecallPlan,
    top_k: int | None = None,
    memory_backend: LanceDbMemoryBackend | None = None,
) -> RetrievalBundle:
    queries = [q for q in plan.fts_queries if str(q).strip()]
    fact_keys = [fk for fk in plan.fact_keys if str(fk).strip()]
    limits = _adaptive_limits(plan, top_k)

    surface_candidates = _search_memory(
        db,
        memory_backend,
        session_key=session_key,
        queries=queries,
        fact_keys=[],
        layer="surface",
        limit=max(6, limits["surface"] * 4),
        kinds=["fact", "event", "digest"],
        include_global=False,
    ) if plan.retrieval.allow_surface else []
    surface = _rerank_memory_results(surface_candidates, plan, preferred_layer="surface", limit=limits["surface"]) if surface_candidates else []

    deep_candidates = _search_memory(
        db,
        memory_backend,
        session_key=session_key,
        queries=queries,
        fact_keys=fact_keys,
        layer="deep",
        limit=max(6, limits["deep"] * 4),
        kinds=["fact"],
        include_global=True,
    ) if plan.retrieval.allow_deep else []
    deep = _rerank_memory_results(deep_candidates, plan, preferred_layer="deep", limit=limits["deep"]) if deep_candidates else []

    timeline: list[dict[str, Any]] = []
    if plan.retrieval.allow_timeline and plan.time_range is not None:
        timeline_candidates = _search_timeline(
            db,
            memory_backend,
            session_key=session_key,
            queries=queries or [plan.time_range.label],
            start_day=plan.time_range.start_day,
            end_day=plan.time_range.end_day,
            limit=max(6, limits["events"] * 4),
        )
        timeline = _rerank_events(timeline_candidates, plan, limit=limits["events"])

    qstyle_for_snapshot: dict[str, str] = {}
    if memory_backend is not None and memory_backend.enabled():
        rows = memory_backend.list_entries(session_key=session_key, kinds=["style"], include_global=True, limit=64)
        for row in rows:
            fact_key = str(row.get("fact_key") or "")
            if not fact_key.startswith("qstyle."):
                continue
            key = fact_key.replace("qstyle.", "", 1)
            if key and key not in qstyle_for_snapshot:
                qstyle_for_snapshot[key] = str(row.get("value") or "")
    anchors = {
        "wm.surf": surface_anchor(db, memory_backend, session_key),
        "wm.deep": deep_anchor(db, memory_backend, session_key),
        "p.snapshot": qctx_profile_snapshot(db, memory_backend, session_key),
    }
    if plan.intent.profile >= 0.45 and not deep:
        fallback_candidates = _search_memory(
            db,
            memory_backend,
            session_key=session_key,
            queries=[],
            fact_keys=["profile.identity.card"],
            layer="deep",
            limit=max(2, limits["deep"] * 2),
            kinds=["fact"],
            include_global=True,
        )
        deep = _rerank_memory_results(fallback_candidates, plan, preferred_layer="deep", limit=limits["deep"]) or deep
    return RetrievalBundle(
        surface=surface,
        deep=deep,
        timeline=timeline,
        anchors=anchors,
        debug={
            "limits": limits,
            "queryCount": len(queries),
            "factKeyCount": len(fact_keys),
            "surfaceHits": len(surface),
            "deepHits": len(deep),
            "timelineHits": len(timeline),
            "memoryBackend": "memory-lancedb-pro" if memory_backend is not None and memory_backend.enabled() else "sqlite",
        },
    )
