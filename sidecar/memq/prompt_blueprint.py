from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sidecar.memq.brain.schemas import BrainRecallPlan
from sidecar.memq.config import Config
from sidecar.memq.db import MemqDB
from sidecar.memq.lancedb_bridge import LanceDbMemoryBackend
from sidecar.memq.local_overrides import load_local_overrides
from sidecar.memq.memory_source import deep_anchor, list_qrule, list_qstyle, profile_snapshot, recent_digest, surface_anchor
from sidecar.memq.memctx_pack import build_memctx, build_memrules, build_memstyle
from sidecar.memq.retrieval import retrieve_with_plan


@dataclass(frozen=True)
class PromptBlueprintBudgets:
    memctx_tokens: int
    rules_tokens: int
    style_tokens: int


@dataclass(frozen=True)
class PromptBlueprintRequest:
    session_key: str
    prompt: str
    recent_messages: list[dict[str, Any]]
    budgets: PromptBlueprintBudgets
    top_k: int
    now_iso: str | None = None


@dataclass(frozen=True)
class PromptBlueprint:
    qrule: str
    qstyle: str
    qctx: str
    meta: dict[str, Any]

    def to_response(self) -> dict[str, Any]:
        return {
            "ok": True,
            "qrule": self.qrule,
            "qstyle": self.qstyle,
            "qctx": self.qctx,
            "meta": self.meta,
        }


class BrainPlanningError(RuntimeError):
    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


def fallback_recall_plan(request: PromptBlueprintRequest) -> BrainRecallPlan:
    query = " ".join(str(request.prompt or "").split())
    if not query:
        query = "recent conversation"
    return BrainRecallPlan.model_validate(
        {
            "fts_queries": [query[:160]],
            "budget_split": {
                "profile": 24,
                "timeline": 24,
                "surface": 36,
                "deep": 24,
                "ephemeral": 0,
            },
        }
    )


def _rewrite_public_labels(text: str) -> str:
    return (
        str(text or "")
        .replace("MEMRULES", "QRULE")
        .replace("MEMRULE", "QRULE")
        .replace("MEMSTYLE", "QSTYLE")
        .replace("MEMCTX", "QCTX")
    )


def _brain_stats(brain: Any) -> dict[str, Any]:
    if hasattr(brain, "stats") and callable(brain.stats):
        try:
            return dict(brain.stats() or {})
        except Exception:
            return {}
    return {}


def _effective_profile_snapshot(snapshot: str, style: dict[str, str]) -> str:
    return _rewrite_public_labels(snapshot)


async def build_prompt_blueprint(
    *,
    cfg: Config,
    db: MemqDB,
    brain: Any,
    request: PromptBlueprintRequest,
    memory_backend: LanceDbMemoryBackend | None = None,
) -> PromptBlueprint:
    overrides = load_local_overrides(cfg.root)
    style = {**list_qstyle(db, memory_backend, request.session_key), **overrides.qstyle}
    rules = {**list_qrule(db, memory_backend, request.session_key), **overrides.qrule}
    recent = list(request.recent_messages)[-6:]
    now_iso = request.now_iso or datetime.now().astimezone().isoformat()
    used_fallback = False
    fallback_reason = ""

    try:
        plan, trace_id, stats = await brain.build_recall_plan(
            session_key=request.session_key,
            prompt=request.prompt,
            recent_messages=recent,
            current_style=style,
            current_rules=rules,
            now_iso=now_iso,
        )
    except Exception as exc:
        if cfg.brain_required:
            raise BrainPlanningError(exc) from exc
        plan = fallback_recall_plan(request)
        trace_id = ""
        stats = {}
        used_fallback = True
        fallback_reason = f"{type(exc).__name__}:{exc}"

    bundle = retrieve_with_plan(
        db,
        session_key=request.session_key,
        plan=plan,
        top_k=request.top_k,
        memory_backend=memory_backend,
    )
    bundle.anchors["p.snapshot"] = profile_snapshot(db, memory_backend, request.session_key, style)
    bundle.anchors["wm.surf"] = surface_anchor(db, memory_backend, request.session_key)
    bundle.anchors["wm.deep"] = deep_anchor(db, memory_backend, request.session_key)
    bundle.anchors["t.recent"] = recent_digest(db, memory_backend, request.session_key, days=2)
    memrules = _rewrite_public_labels(build_memrules(rules, request.budgets.rules_tokens))
    memstyle = _rewrite_public_labels(build_memstyle(style, request.budgets.style_tokens))
    memctx = _rewrite_public_labels(build_memctx(plan, bundle, request.budgets.memctx_tokens))
    used_ids = [item.id for item in bundle.surface] + [item.id for item in bundle.deep]
    qctx_keys = [line.split("=", 1)[0] for line in memctx.splitlines() if "=" in line]
    brain_stats = _brain_stats(brain)

    return PromptBlueprint(
        qrule=memrules,
        qstyle=memstyle,
        qctx=memctx,
        meta={
            "surfaceHit": bool(bundle.surface),
            "deepCalled": plan.retrieval.allow_deep,
            "usedMemoryIds": used_ids,
            "debug": {
                "trace_id": trace_id,
                "brain_latency_ms": stats.get("total_duration"),
                "ps_seen": 1 if brain_stats.get("last_ps_seen_model") else 0,
                "intent": plan.intent.model_dump(),
                "time_range": plan.time_range.model_dump() if plan.time_range else None,
                "qctx_keys": qctx_keys,
                "retrieval": bundle.debug,
                "qctx_backend": "memory-lancedb-pro" if memory_backend is not None and memory_backend.enabled() else "sqlite",
                "qstyle_override_keys": sorted(overrides.qstyle.keys()),
                "qrule_override_keys": sorted(overrides.qrule.keys()),
                "source": "fallback" if used_fallback else "brain",
                "fallback_reason": fallback_reason,
            },
        },
    )
