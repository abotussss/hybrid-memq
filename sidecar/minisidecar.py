from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

from fastapi import FastAPI, Query

from memq.audit import audit_output
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
from memq.structured_facts import (
    extract_structured_facts_from_text,
    is_durable_fact_text,
    normalize_fact_value,
    parse_fact_signature_from_row,
    structured_fact_summary,
)


app = FastAPI(title="hybrid-memq-sidecar", version="2.0.0")
cfg: MemqConfig = load_config()
db = MemqDB(cfg.db_path)
state_lock = Lock()
state: Dict[str, Any] = {
    "started_at": int(time.time()),
    "last_activity_at": int(time.time()),
    "last_consolidation_at": 0,
    "last_session_key": "default",
    "idle_runs": 0,
}

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
        },
        "state": snapshot,
    }


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
    wrote = ingest_turn(
        db=db,
        session_key=req.sessionKey,
        user_text=req.userText,
        assistant_text=req.assistantText,
        ts=req.ts,
        dim=cfg.dim,
        bits_per_dim=cfg.bits_per_dim,
    )
    refresh_preference_profiles(db, int(time.time()))
    return IngestTurnResponse(ok=True, wrote=wrote)


@app.post("/memctx/query", response_model=MemctxQueryResponse)
def memctx_query(req: MemctxQueryRequest) -> MemctxQueryResponse:
    _touch(req.sessionKey)

    top_k = max(1, int(req.topK or cfg.retrieval_top_k))
    surface_threshold = float(req.surfaceThreshold if req.surfaceThreshold is not None else cfg.surface_threshold)
    deep_enabled = bool(req.deepEnabled if req.deepEnabled is not None else cfg.deep_enabled)
    surf, deep, meta = retrieve_candidates(
        db=db,
        session_key=req.sessionKey,
        prompt=req.prompt,
        dim=cfg.dim,
        bits_per_dim=cfg.bits_per_dim,
        top_k=top_k,
        surface_threshold=surface_threshold,
        deep_enabled=deep_enabled,
    )

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
        ),
    )


@app.post("/idle/run_once", response_model=IdleRunResponse)
def idle_run_once(req: IdleRunRequest) -> IdleRunResponse:
    now = int(req.nowTs or time.time())
    with state_lock:
        session_key = str(state.get("last_session_key", "default"))
    res = run_idle_consolidation(db, session_key=session_key, dim=cfg.dim, bits_per_dim=cfg.bits_per_dim)
    with state_lock:
        state["last_consolidation_at"] = now
        state["idle_runs"] = int(state.get("idle_runs", 0)) + 1
        state["last_idle_result"] = res
    return IdleRunResponse(ok=True, did=list(res.get("did", [])), stats=dict(res.get("stats", {})))


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
