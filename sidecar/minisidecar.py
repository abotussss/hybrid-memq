from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from memq.audit import audit_output
from memq.brain import BrainService
from memq.brain.ollama_client import BrainUnavailable
from memq.config import MemqConfig, load_config
from memq.conv_summarize import deep_summary_candidates, merge_summary, summarize_for_deep, summarize_for_surface
from memq.db import MemqDB
from memq.idle_consolidation import run_idle_consolidation
from memq.ingest import ingest_turn
from memq.ingest_md import import_markdown_memory
from memq.memctx_pack import build_memctx, build_memrules, build_memstyle
from memq.models import (
    AuditRequest,
    AuditResponse,
    BootstrapImportRequest,
    IdleRunRequest,
    IdleRunResponse,
    IdleTickRequest,
    IngestTurnRequest,
    IngestTurnResponse,
    MemctxMeta,
    MemctxQueryRequest,
    MemctxQueryResponse,
    ProfileResponse,
    QuarantineResponse,
    SummarizeRequest,
    SummarizeResponse,
)
from memq.retrieval import retrieve_candidates
from memq.rules import refresh_preference_profiles
from memq.style import sanitize_style_profile
from memq.structured_facts import (
    extract_structured_facts_from_text,
    is_durable_fact_text,
    normalize_fact_value,
    parse_fact_signature_from_row,
    structured_fact_summary,
)
from memq.timeline import TimelineRange, detect_timeline_range


app = FastAPI(title="hybrid-memq-sidecar", version="2.0.0")
cfg: MemqConfig = load_config()
db = MemqDB(cfg.db_path)
brain = BrainService(cfg)
state_lock = Lock()
state: Dict[str, Any] = {
    "started_at": int(time.time()),
    "last_activity_at": int(time.time()),
    "last_consolidation_at": 0,
    "last_session_key": "default",
    "idle_runs": 0,
}


def _brain_mode_required() -> bool:
    return str(cfg.brain_mode or "best_effort").lower() == "required"


def _brain_error_response(
    *,
    code: str,
    op: str,
    session_key: str,
    trace_id: str,
    err: Exception | str,
) -> JSONResponse:
    err_msg = str(err or code)
    err_type = type(err).__name__ if isinstance(err, Exception) else "BrainError"
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "code": code,
            "op": op,
            "session_key": session_key,
            "provider": cfg.brain_provider,
            "model": cfg.brain_model,
            "trace_id": trace_id,
            "err_type": err_type,
            "err_msg": err_msg,
        },
    )


def _brain_code(default: str, err: Exception | str) -> str:
    msg = str(err or "")
    if "brain_proof_failed" in msg:
        return "brain_proof_failed"
    if "brain_cooldown" in msg:
        return "brain_cooldown"
    if "brain_apply_failed" in msg:
        return "brain_apply_failed"
    return default


def _ensure_brain_or_error(session_key: str, *, op: str, required: bool) -> JSONResponse | None:
    if not required:
        return None
    try:
        status = brain.ensure_runtime(session_key=session_key)
    except Exception as e:
        return _brain_error_response(
            code=_brain_code("brain_unavailable", e),
            op=op,
            session_key=session_key,
            trace_id=brain.last_trace_id(op),
            err=e,
        )
    if not bool(status.get("seen")):
        return _brain_error_response(
            code="brain_unavailable",
            op=op,
            session_key=session_key,
            trace_id=str(status.get("trace_id") or brain.last_trace_id(op)),
            err=str(status.get("err") or "brain_runtime_not_ready"),
        )
    return None

def _has_fact(session_key: str, fact_key: str, value: str, limit: int = 6000) -> bool:
    value_l = normalize_fact_value(value).lower()
    if not fact_key or not value_l:
        return False
    indexed = db.fetch_deep_items_by_fact_keys(
        session_key=session_key,
        fact_keys=[fact_key],
        limit=max(256, min(2000, limit)),
        include_global=True,
    )
    for r in indexed:
        fk, fv = parse_fact_signature_from_row(dict(r))
        if fk == fact_key and fv == value_l:
            return True
    for r in db.list_memory_items("deep", session_key, limit=limit):
        fk, fv = parse_fact_signature_from_row(dict(r))
        if fk == fact_key and fv == value_l:
            return True
    return False


def _promote_deep_candidates(session_key: str, candidates: List[str]) -> int:
    now = int(time.time())
    wrote = 0
    for c in candidates:
        facts = extract_structured_facts_from_text(c, ts=now, source="conv_summarize")
        if not facts:
            continue
        durable = is_durable_fact_text(c)
        for fact in facts:
            fk = str(fact.get("fact_key") or "")
            fv = str(fact.get("value") or "")
            if _has_fact(session_key, fk, fv):
                continue
            summary = structured_fact_summary(fact)
            item_id = db.add_memory_item(
                session_key=session_key,
                layer="deep",
                text=summary,
                summary=summary,
                importance=0.72,
                tags={"kind": "structured_fact", "from": "pruned", "ts": now, "fact_keys": [fk], "fact": fact},
                emb_f16=None,
                emb_q=None,
                emb_dim=0,
                source="conv_summarize",
            )
            if fk:
                wrote += db.expire_conflicting_fact_keys("deep", session_key, [fk], item_id)
            wrote += 1
            if durable and not _has_fact("global", fk, fv):
                gid = db.add_memory_item(
                    session_key="global",
                    layer="deep",
                    text=summary,
                    summary=summary,
                    importance=0.78,
                    tags={"kind": "durable_global_fact", "from": "pruned", "ts": now, "fact_keys": [fk], "fact": fact},
                    emb_f16=None,
                    emb_q=None,
                    emb_dim=0,
                    source="conv_summarize",
                )
                if fk:
                    wrote += db.expire_conflicting_fact_keys(
                        "deep",
                        "global",
                        [fk],
                        gid,
                    )
                wrote += 1
    return wrote


def _touch(session_key: str) -> None:
    with state_lock:
        state["last_activity_at"] = int(time.time())
        state["last_session_key"] = session_key


def _idle_merge_with_brain(session_key: str, *, required: bool) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    rows = db.list_memory_items("deep", session_key, limit=800)
    if len(rows) < 240:
        rows = db.list_memory_items_any("deep", limit=1200)
    for r in rows[:1200]:
        candidates.append(
            {
                "id": str(r["id"]),
                "session_key": str(r["session_key"]),
                "layer": str(r["layer"]),
                "summary": str(r["summary"] or ""),
                "updated_at": int(r["updated_at"] or 0),
                "importance": float(r["importance"] or 0.0),
                "usage_count": int(r["usage_count"] or 0),
            }
        )
    attempts = 3 if required else 1
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            plan = brain.build_merge_plan(
                session_key=session_key,
                memory_candidates=candidates,
                stats=db.memory_stats(),
            )
            trace_id = brain.last_trace_id("merge_plan")
            if plan is None:
                if required:
                    raise BrainUnavailable("required_mode_no_merge_plan")
                return {"trace_id": trace_id, "applied": {"merged": 0, "pruned": 0, "quarantined": 0}}
            applied = brain.apply_merge_plan(db=db, session_key=session_key, plan=plan)
            brain.record_apply(op="merge_plan", session_key=session_key, trace_id=trace_id, apply_summary=applied)
            return {"trace_id": trace_id, "applied": applied}
        except BrainUnavailable as e:
            last_err = e
            # required mode must remain fail-closed, but allow short transient retries.
            if i + 1 >= attempts:
                break
            try:
                brain.ensure_runtime(session_key=session_key)
            except Exception:
                pass
            time.sleep(0.6 * (i + 1))
            continue
    if last_err is not None:
        raise last_err
    raise BrainUnavailable("merge_plan_unknown_failure")


async def _idle_loop() -> None:
    sleep_sec = max(5, min(30, cfg.idle_seconds // 2))
    while True:
        await asyncio.sleep(sleep_sec)
        if not cfg.idle_enabled:
            continue
        now = int(time.time())
        with state_lock:
            last_act = int(state.get("last_activity_at", now))
            last_run = int(state.get("last_consolidation_at", 0))
            session_key = str(state.get("last_session_key", "default"))
        if now - last_act < cfg.idle_seconds:
            continue
        if now - last_run < max(30, cfg.idle_seconds // 2):
            continue
        res = run_idle_consolidation(db, session_key=session_key, dim=cfg.dim, bits_per_dim=cfg.bits_per_dim)
        try:
            merge = _idle_merge_with_brain(session_key, required=_brain_mode_required())
            res.setdefault("stats", {})
            res["stats"]["brain_merge"] = merge.get("applied", {})
            res["stats"]["brain_merge_trace_id"] = str(merge.get("trace_id") or "")
            did = list(res.get("did", []))
            if "brain_merge_plan" not in did:
                did.append("brain_merge_plan")
            res["did"] = did
        except Exception as e:
            if _brain_mode_required():
                with state_lock:
                    state["last_idle_error"] = str(e)
                continue
        with state_lock:
            state["last_consolidation_at"] = now
            state["idle_runs"] = int(state.get("idle_runs", 0)) + 1
            state["last_idle_result"] = res


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(_idle_loop())


@app.get("/health")
def health() -> Dict[str, Any]:
    with state_lock:
        snapshot = dict(state)
    return {
        "ok": True,
        "version": app.version,
        "db": str(cfg.db_path),
        "index": "bruteforce-q",
        "config": {
            "dim": cfg.dim,
            "bitsPerDim": cfg.bits_per_dim,
            "idleEnabled": cfg.idle_enabled,
            "idleSeconds": cfg.idle_seconds,
            "brainEnabled": cfg.brain_enabled,
            "brainProvider": cfg.brain_provider,
            "brainModel": cfg.brain_model,
            "brainMode": cfg.brain_mode,
        },
        "brain": brain.status,
        "state": snapshot,
    }


@app.get("/brain/stats")
def brain_stats() -> Dict[str, Any]:
    return {"ok": True, **brain.stats}


@app.get("/brain/trace/recent")
def brain_trace_recent(n: int = Query(default=50, ge=1, le=500)) -> Dict[str, Any]:
    return {"ok": True, "items": brain.recent_traces(n)}


@app.post("/brain/ensure")
def brain_ensure(sessionKey: str = Query(default="runtime")) -> Dict[str, Any]:
    return brain.ensure_runtime(session_key=sessionKey)


@app.post("/idle_tick")
def idle_tick(req: IdleTickRequest) -> Dict[str, Any]:
    now = int(req.nowSec or time.time())
    with state_lock:
        state["last_activity_at"] = now
    return {"ok": True, "last_activity_at": now}


@app.post("/bootstrap/import_md")
def bootstrap_import_md(req: BootstrapImportRequest) -> Dict[str, Any]:
    root = Path(req.workspaceRoot)
    wrote = import_markdown_memory(db, root, cfg.dim, cfg.bits_per_dim)
    return {"ok": True, "wrote": wrote}


@app.post("/conversation/summarize", response_model=SummarizeResponse)
def conversation_summarize(req: SummarizeRequest) -> SummarizeResponse:
    _touch(req.sessionKey)
    messages = req.prunedMessages
    if req.retentionScope == "surface_only":
        new_summary = summarize_for_surface(messages)
        old = db.get_conv_summary(req.sessionKey, "surface_only") or ""
        merged = merge_summary(old, new_summary, max_chars=1200)
        cid = db.upsert_conv_summary(req.sessionKey, "surface_only", merged)
        return SummarizeResponse(ok=True, convsurfId=cid, stats={"lines": len(merged.splitlines())})

    new_summary = summarize_for_deep(messages)
    old = db.get_conv_summary(req.sessionKey, "deep") or ""
    merged = merge_summary(old, new_summary, max_chars=2400)
    cid = db.upsert_conv_summary(req.sessionKey, "deep", merged)
    promoted = _promote_deep_candidates(req.sessionKey, deep_summary_candidates(new_summary, max_lines=8))
    return SummarizeResponse(ok=True, convdeepId=cid, stats={"lines": len(merged.splitlines()), "promotedDeep": promoted})


@app.post("/memory/ingest_turn", response_model=IngestTurnResponse)
def memory_ingest_turn(req: IngestTurnRequest) -> IngestTurnResponse:
    _touch(req.sessionKey)
    wrote: Dict[str, int] | None = None
    brain_used = False
    required = _brain_mode_required()
    ensure_err = _ensure_brain_or_error(req.sessionKey, op="ingest_plan", required=required)
    if ensure_err is not None:
        return ensure_err
    try:
        plan = brain.build_ingest_plan(
            session_key=req.sessionKey,
            user_text=req.userText,
            assistant_text=req.assistantText,
            ts=req.ts,
            metadata=req.metadata,
        )
    except Exception as e:
        return _brain_error_response(
            code=_brain_code("brain_unavailable", e),
            op="ingest_plan",
            session_key=req.sessionKey,
            trace_id=brain.last_trace_id("ingest_plan"),
            err=e,
        )

    if plan is not None:
        try:
            wrote = brain.apply_ingest_plan(
                db=db,
                session_key=req.sessionKey,
                ts=req.ts,
                plan=plan,
                user_text=req.userText,
                assistant_text=req.assistantText,
                metadata=req.metadata,
            )
            brain.record_apply(
                op="ingest_plan",
                session_key=req.sessionKey,
                trace_id=brain.last_trace_id("ingest_plan"),
                apply_summary=wrote,
            )
            brain_used = True
        except Exception as e:
            if required:
                return _brain_error_response(
                    code=_brain_code("brain_apply_failed", e),
                    op="ingest_apply",
                    session_key=req.sessionKey,
                    trace_id=brain.last_trace_id("ingest_plan"),
                    err=e,
                )
            wrote = None
    elif required:
        return _brain_error_response(
            code="brain_unavailable",
            op="ingest_plan",
            session_key=req.sessionKey,
            trace_id=brain.last_trace_id("ingest_plan"),
            err="required_mode_no_plan",
        )

    if wrote is None:
        if required:
            return _brain_error_response(
                code="brain_unavailable",
                op="ingest_plan",
                session_key=req.sessionKey,
                trace_id=brain.last_trace_id("ingest_plan"),
                err="required_mode_fallback_blocked",
            )
        wrote = ingest_turn(
            db=db,
            session_key=req.sessionKey,
            user_text=req.userText,
            assistant_text=req.assistantText,
            ts=req.ts,
            dim=cfg.dim,
            bits_per_dim=cfg.bits_per_dim,
            metadata=req.metadata,
        )
    refresh_preference_profiles(db, int(time.time()))
    wrote["brain"] = 1 if brain_used else 0
    return IngestTurnResponse(ok=True, wrote=wrote, traceId=brain.last_trace_id("ingest_plan"))


@app.post("/memctx/query", response_model=MemctxQueryResponse)
def memctx_query(req: MemctxQueryRequest) -> MemctxQueryResponse:
    _touch(req.sessionKey)
    sanitize_style_profile(db)

    top_k = max(1, int(req.topK or cfg.retrieval_top_k))
    surface_threshold = float(req.surfaceThreshold if req.surfaceThreshold is not None else cfg.surface_threshold)
    deep_enabled = bool(req.deepEnabled if req.deepEnabled is not None else cfg.deep_enabled)
    timeline_range: TimelineRange | None = None
    timeline_first = False
    required = _brain_mode_required()
    ensure_err = _ensure_brain_or_error(req.sessionKey, op="recall_plan", required=required)
    if ensure_err is not None:
        return ensure_err
    recent_messages = [
        {"role": str(m.role), "text": str(m.text), "ts": int(m.ts) if m.ts is not None else None}
        for m in (req.recentMessages or [])
    ]
    try:
        brain_plan_obj = brain.build_recall_plan(
            session_key=req.sessionKey,
            prompt=req.prompt,
            recent_messages=recent_messages,
            budgets={
                "memctxTokens": max(16, int(req.budgets.memctxTokens)),
                "rulesTokens": max(8, int(req.budgets.rulesTokens)),
                "styleTokens": max(8, int(req.budgets.styleTokens)),
            },
            top_k=top_k,
            surface_threshold=surface_threshold,
            deep_enabled=deep_enabled,
        )
    except Exception as e:
        return _brain_error_response(
            code=_brain_code("brain_unavailable", e),
            op="recall_plan",
            session_key=req.sessionKey,
            trace_id=brain.last_trace_id("recall_plan"),
            err=e,
        )
    brain_plan: Dict[str, Any] | None = brain_plan_obj.model_dump() if brain_plan_obj is not None else None
    if isinstance(brain_plan, dict):
        tr = brain_plan.get("time_range")
        if isinstance(tr, dict):
            sd = str(tr.get("startDay") or "").strip()
            ed = str(tr.get("endDay") or "").strip()
            label = str(tr.get("label") or "brain")
            if sd and ed:
                timeline_range = TimelineRange(start_day=sd, end_day=ed, label=label, explicit=True)
                timeline_first = True
    if timeline_range is None:
        timeline_range = detect_timeline_range(req.prompt)
        timeline_first = bool(timeline_range and timeline_range.explicit)

    if brain_plan is not None:
        from memq.retrieval import retrieve_candidates_with_plan

        surf, deep, meta = retrieve_candidates_with_plan(
            db=db,
            session_key=req.sessionKey,
            prompt=req.prompt,
            dim=cfg.dim,
            bits_per_dim=cfg.bits_per_dim,
            top_k=top_k,
            surface_threshold=surface_threshold,
            deep_enabled=deep_enabled,
            plan=brain_plan,
        )
    else:
        if required:
            return _brain_error_response(
                code="brain_unavailable",
                op="recall_plan",
                session_key=req.sessionKey,
                trace_id=brain.last_trace_id("recall_plan"),
                err="required_mode_no_plan",
            )
        # Keep deep fallback available even for explicit timeline prompts.
        # Timeline blocks are prioritized in MEMCTX packing, but deep can rescue
        # missing/immature digests so "昨日何した？" does not return empty context.
        deep_enabled_for_retrieval = deep_enabled
        top_k_for_retrieval = max(1, min(top_k, 3)) if timeline_first else top_k
        surf, deep, meta = retrieve_candidates(
            db=db,
            session_key=req.sessionKey,
            prompt=req.prompt,
            dim=cfg.dim,
            bits_per_dim=cfg.bits_per_dim,
            top_k=top_k_for_retrieval,
            surface_threshold=surface_threshold,
            deep_enabled=deep_enabled_for_retrieval,
        )

    dbg = dict(meta.get("debug") or {})
    call_meta = brain.last_call_meta("recall_plan")
    dbg["timeline_route"] = 1 if timeline_first else 0
    dbg["timeline_label"] = timeline_range.label if timeline_range else ""
    dbg["brain_enabled"] = 1 if brain.enabled else 0
    dbg["brain_required"] = 1 if required else 0
    dbg["brain_plan"] = 1 if brain_plan is not None else 0
    dbg["trace_id"] = str(call_meta.get("trace_id") or brain.last_trace_id("recall_plan"))
    dbg["ps_seen"] = 1 if bool(call_meta.get("ps_seen")) else 0
    dbg["brain_call_ok"] = 1 if bool(call_meta.get("ok")) else 0
    dbg["brain_latency_ms"] = int(call_meta.get("latency_ms") or 0)
    if brain_plan and isinstance(brain_plan.get("intent"), dict):
        for k, v in brain_plan["intent"].items():
            dbg[f"brain_intent_{k}"] = v
    if brain_plan and isinstance(brain_plan.get("time_range"), dict):
        dbg["brain_time_range"] = f"{brain_plan['time_range'].get('startDay','')}..{brain_plan['time_range'].get('endDay','')}"
    meta["debug"] = dbg

    used_ids = [x["id"] for x in surf] + [x["id"] for x in deep]
    db.touch_items(used_ids)

    b = req.budgets
    memrules = build_memrules(db, max(8, int(b.rulesTokens)))
    memstyle = build_memstyle(db, max(8, int(b.styleTokens)))
    memctx = build_memctx(
        db=db,
        session_key=req.sessionKey,
        prompt=req.prompt,
        surface=surf,
        deep=deep,
        budget_tokens=max(16, int(b.memctxTokens)),
        brain_plan=brain_plan,
    )

    return MemctxQueryResponse(
        ok=True,
        memrules=memrules,
        memstyle=memstyle,
        memctx=memctx,
        meta=MemctxMeta(
            surfaceHit=bool(meta.get("surfaceHit")),
            deepCalled=bool(meta.get("deepCalled")),
            usedMemoryIds=used_ids,
            debug=dict(meta.get("debug") or {}),
            traceId=brain.last_trace_id("recall_plan"),
        ),
        traceId=brain.last_trace_id("recall_plan"),
    )


@app.post("/idle/run_once", response_model=IdleRunResponse)
def idle_run_once(req: IdleRunRequest) -> IdleRunResponse:
    now = int(req.nowTs or time.time())
    with state_lock:
        session_key = str(state.get("last_session_key", "default"))
    required = _brain_mode_required()
    ensure_err = _ensure_brain_or_error(session_key, op="merge_plan", required=required)
    if ensure_err is not None:
        return ensure_err
    res = run_idle_consolidation(db, session_key=session_key, dim=cfg.dim, bits_per_dim=cfg.bits_per_dim)
    try:
        merge = _idle_merge_with_brain(session_key, required=required)
    except Exception as e:
        return _brain_error_response(
            code=_brain_code("brain_unavailable", e),
            op="merge_plan",
            session_key=session_key,
            trace_id=brain.last_trace_id("merge_plan"),
            err=e,
        )
    res.setdefault("stats", {})
    res["stats"]["brain_merge"] = merge.get("applied", {})
    res["stats"]["brain_merge_trace_id"] = str(merge.get("trace_id") or "")
    did = list(res.get("did", []))
    if "brain_merge_plan" not in did:
        did.append("brain_merge_plan")
    res["did"] = did
    with state_lock:
        state["last_consolidation_at"] = now
        state["idle_runs"] = int(state.get("idle_runs", 0)) + 1
        state["last_idle_result"] = res
    return IdleRunResponse(
        ok=True,
        did=list(res.get("did", [])),
        stats=dict(res.get("stats", {})),
        traceId=str(merge.get("trace_id") or ""),
    )


@app.post("/audit/output", response_model=AuditResponse)
def audit_output_ep(req: AuditRequest) -> AuditResponse:
    _touch(req.sessionKey)
    th = req.thresholds
    llm_threshold = float(th.llmAuditThreshold if th else 0.2)
    block_threshold = float(th.blockThreshold if th else 0.85)
    res = audit_output(
        db=db,
        config=cfg,
        session_key=req.sessionKey,
        text=req.text,
        mode=req.mode,
        llm_audit_threshold=llm_threshold,
        block_threshold=block_threshold,
    )
    return AuditResponse(ok=True, risk=res.risk, block=res.block, redactedText=res.redacted_text, reasons=res.reasons)


@app.get("/profile", response_model=ProfileResponse)
def profile() -> ProfileResponse:
    return ProfileResponse(
        ok=True,
        style_profile=db.get_style_profile(),
        preference_profile=db.get_preference_profile(),
        memory_policy_profile=db.get_memory_policy_profile(),
    )


@app.get("/quarantine", response_model=QuarantineResponse)
def quarantine(limit: int = Query(default=50, ge=1, le=500)) -> QuarantineResponse:
    return QuarantineResponse(ok=True, items=db.get_quarantine(limit=limit))


@app.get("/memory/stats")
def memory_stats() -> Dict[str, Any]:
    return {"ok": True, "stats": db.memory_stats()}


@app.get("/memory/list")
def memory_list(
    layer: str | None = Query(default=None),
    sessionKey: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=500),
) -> Dict[str, Any]:
    return {"ok": True, "items": db.list_memory_debug(layer=layer, session_key=sessionKey, limit=limit)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("minisidecar:app", host="127.0.0.1", port=7781, reload=False)
