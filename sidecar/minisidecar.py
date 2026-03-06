from __future__ import annotations

from datetime import datetime
import contextlib
from pathlib import Path
import asyncio
import json
import os
import signal
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import uvicorn

from sidecar.memq.audit import audit_output
from sidecar.memq.brain.ollama_client import BrainUnavailable
from sidecar.memq.brain.schemas import BrainIngestPlan, BrainRecallPlan
from sidecar.memq.brain.service import BrainService, explicit_rule_requested, explicit_style_requested
from sidecar.memq.config import Config, load_config
from sidecar.memq.db import MemqDB
from sidecar.memq.idle_consolidation import run_idle_consolidation
from sidecar.memq.memctx_pack import build_memctx, build_memrules, build_memstyle
from sidecar.memq.retrieval import retrieve_with_plan


class Message(BaseModel):
    role: str
    text: str
    ts: int | None = None


class QueryBudgets(BaseModel):
    memctxTokens: int
    rulesTokens: int
    styleTokens: int


class QueryRequest(BaseModel):
    sessionKey: str
    prompt: str
    recentMessages: list[Message] = Field(default_factory=list)
    budgets: QueryBudgets
    topK: int = 5


class IngestRequest(BaseModel):
    sessionKey: str
    userText: str
    assistantText: str
    ts: int
    metadata: dict[str, Any] | None = None


class SummarizeRequest(BaseModel):
    sessionKey: str
    prunedMessages: list[Message]
    retentionScope: str


class IdleRequest(BaseModel):
    nowTs: int | None = None
    maxWorkMs: int | None = None


class AuditRequest(BaseModel):
    sessionKey: str
    text: str
    mode: str = "primary"
    thresholds: dict[str, float] | None = None


cfg: Config = load_config()
db = MemqDB(cfg.db_path, timezone_name=cfg.timezone)
brain = BrainService(cfg)
app = FastAPI(title="Hybrid MEMQ v3")
last_activity_at = 0
idle_task: asyncio.Task[Any] | None = None
idle_failure = ""


def _fallback_ingest_plan(req: IngestRequest) -> BrainIngestPlan:
    payload: dict[str, Any] = {
        "events": [
            {
                "actor": "user",
                "kind": "chat",
                "summary": req.userText[:160] or req.assistantText[:160] or "turn",
                "salience": 0.4,
                "ttl_days": 14,
            }
        ]
    }
    if explicit_style_requested(req.userText):
        payload["style_update"] = {"apply": False, "explicit": True, "keys": {}}
    if explicit_rule_requested(req.userText):
        payload["rules_update"] = {"apply": False, "explicit": True, "rules": {}}
    return BrainIngestPlan.model_validate(payload)


def _fallback_recall_plan(req: QueryRequest) -> BrainRecallPlan:
    query = " ".join(str(req.prompt or "").split())
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


async def _raise_brain(exc: Exception, *, code: str, op: str, session_key: str, trace_id: str = "") -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "code": code,
            "op": op,
            "session_key": session_key,
            "model": cfg.brain.model,
            "provider": cfg.brain.provider,
            "trace_id": trace_id,
            "err_type": type(exc).__name__,
            "err_msg": str(exc),
        },
    )


async def _idle_loop() -> None:
    global last_activity_at, idle_failure
    while True:
        await asyncio.sleep(max(5, cfg.idle_seconds // 2))
        if not cfg.idle_enabled:
            continue
        now = int(datetime.now().timestamp())
        if last_activity_at and now - last_activity_at < cfg.idle_seconds:
            continue
        try:
            await run_idle_consolidation(cfg=cfg, db=db, brain=brain, session_key="global")
            idle_failure = ""
        except Exception as exc:
            if cfg.brain_required:
                idle_failure = f"{type(exc).__name__}:{exc}"
                raise


@app.on_event("startup")
async def startup() -> None:
    global idle_task
    if cfg.idle_enabled:
        idle_task = asyncio.create_task(_idle_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global idle_task
    if idle_task:
        idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle_task
    await brain.close()
    db.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "config": {
            "brainMode": cfg.brain.mode,
            "brainModel": cfg.brain.model,
            "brainProvider": cfg.brain.provider,
            "timezone": cfg.timezone,
            "budgets": {
                "memctx": cfg.budgets.memctx_tokens,
                "rules": cfg.budgets.rules_tokens,
                "style": cfg.budgets.style_tokens,
            },
        },
        "brain": brain.stats(),
        "db": str(cfg.db_path),
        "idle": {
            "enabled": cfg.idle_enabled,
            "failed": bool(idle_failure),
            "lastError": idle_failure,
        },
    }


@app.post("/idle_tick")
async def idle_tick(payload: dict[str, Any]) -> dict[str, Any]:
    global last_activity_at
    last_activity_at = int(payload.get("nowSec") or datetime.now().timestamp())
    return {"ok": True}


@app.post("/bootstrap/import_md")
async def bootstrap_import_md(payload: dict[str, Any]) -> dict[str, Any]:
    workspace_root = Path(str(payload.get("workspaceRoot") or cfg.root)).expanduser().resolve()
    imported = 0
    for name in ("IDENTITY.md", "SOUL.md", "HEARTBEAT.md", "MEMORY.md", "AGENTS.md"):
        path = workspace_root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        fact_key = "profile.identity.card" if name == "IDENTITY.md" else f"project.bootstrap.{name.lower().replace('.', '_')}"
        db.insert_memory(
            session_key="global",
            layer="deep",
            kind="carry",
            fact_key=fact_key,
            value=text[:240],
            text=text,
            summary=text[:240],
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            tags={"source": name},
            source_quote=text[:160],
        )
        imported += 1
    return {"ok": True, "imported": imported}


@app.post("/memory/ingest_turn")
async def memory_ingest_turn(req: IngestRequest) -> dict[str, Any]:
    global last_activity_at
    last_activity_at = req.ts
    style = db.list_style(req.sessionKey)
    rules = db.list_rules(req.sessionKey)
    recent_summary = db.recent_brain_context(req.sessionKey)
    try:
        plan, trace_id, _ = await brain.build_ingest_plan(
            session_key=req.sessionKey,
            user_text=req.userText,
            assistant_text=req.assistantText,
            current_style=style,
            current_rules=rules,
            recent_summary=recent_summary,
        )
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="ingest_plan", session_key=req.sessionKey)
        plan = _fallback_ingest_plan(req)
        trace_id = ""
    wrote = brain.apply_ingest_plan(db, session_key=req.sessionKey, plan=plan, ts=req.ts, user_text=req.userText)
    return {"ok": True, "wrote": wrote, "traceId": trace_id}


@app.post("/conversation/summarize")
async def conversation_summarize(req: SummarizeRequest) -> dict[str, Any]:
    joined = "\n".join(f"{m.role}:{m.text}" for m in req.prunedMessages[-8:])
    if not joined.strip():
        return {"ok": True, "summary": ""}
    try:
        plan, trace_id, _ = await brain.build_ingest_plan(
            session_key=req.sessionKey,
            user_text=joined,
            assistant_text="",
            current_style=db.list_style(req.sessionKey),
            current_rules=db.list_rules(req.sessionKey),
            recent_summary=db.recent_brain_context(req.sessionKey),
        )
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="conversation_summarize", session_key=req.sessionKey)
        raise
    wrote = brain.apply_ingest_plan(db, session_key=req.sessionKey, plan=plan, ts=int(datetime.now().timestamp()), user_text=joined)
    return {"ok": True, "traceId": trace_id, "wrote": wrote}


@app.post("/memctx/query")
async def memctx_query(req: QueryRequest) -> dict[str, Any]:
    global last_activity_at
    last_activity_at = int(datetime.now().timestamp())
    style = db.list_style(req.sessionKey)
    rules = db.list_rules(req.sessionKey)
    recent = [m.model_dump() for m in req.recentMessages][-6:]
    now_iso = datetime.now().astimezone().isoformat()
    try:
        plan, trace_id, stats = await brain.build_recall_plan(
            session_key=req.sessionKey,
            prompt=req.prompt,
            recent_messages=recent,
            current_style=style,
            current_rules=rules,
            now_iso=now_iso,
        )
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="recall_plan", session_key=req.sessionKey)
        plan = _fallback_recall_plan(req)
        trace_id = ""
        stats = {}
    bundle = retrieve_with_plan(db, session_key=req.sessionKey, plan=plan)
    memrules = build_memrules(rules, req.budgets.rulesTokens)
    memstyle = build_memstyle(style, req.budgets.styleTokens)
    memctx = build_memctx(plan, bundle, req.budgets.memctxTokens)
    used_ids = [item.id for item in bundle.surface] + [item.id for item in bundle.deep]
    memctx_keys = [line.split("=", 1)[0] for line in memctx.splitlines() if "=" in line and not line.startswith("budget_tokens=")]
    return {
        "ok": True,
        "memrules": memrules,
        "memstyle": memstyle,
        "memctx": memctx,
        "meta": {
            "surfaceHit": bool(bundle.surface),
            "deepCalled": plan.retrieval.allow_deep,
            "usedMemoryIds": used_ids,
            "debug": {
                "trace_id": trace_id,
                "brain_latency_ms": stats.get("total_duration"),
                "ps_seen": 1 if brain.stats().get("last_ps_seen_model") else 0,
                "intent": plan.intent.model_dump(),
                "time_range": plan.time_range.model_dump() if plan.time_range else None,
                "memctx_keys": memctx_keys,
            },
        },
    }


@app.post("/idle/run_once")
async def idle_run_once(req: IdleRequest) -> dict[str, Any]:
    session_key = "global"
    try:
        stats, trace_id = await run_idle_consolidation(cfg=cfg, db=db, brain=brain, session_key=session_key)
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="merge_plan", session_key=session_key)
        stats = {"did": []}
        trace_id = None
    return {
        "ok": True,
        "did": stats.get("did", []),
        "stats": stats,
        "traceId": trace_id,
        "psSeen": 1 if brain.stats().get("last_ps_seen_model") else 0,
    }


@app.post("/audit/output")
async def audit_output_endpoint(req: AuditRequest) -> dict[str, Any]:
    rules = db.list_rules(req.sessionKey)
    allowed_raw = rules.get("language.allowed", "")
    allowed = [part.strip() for part in allowed_raw.split(",") if part.strip()] if allowed_raw else list(cfg.audit.allowed_languages_default)
    return await audit_output(
        cfg=cfg,
        brain=brain,
        session_key=req.sessionKey,
        text=req.text,
        allowed_languages=allowed,
        mode=req.mode,
    )


@app.get("/profile")
async def profile(session_key: str = Query("global")) -> dict[str, Any]:
    return {
        "ok": True,
        "style_profile": db.list_style(session_key),
        "rules": db.list_rules(session_key),
        "profile_snapshot": db.profile_snapshot(session_key),
    }


@app.get("/quarantine")
async def quarantine(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "items": db.list_quarantine(limit)}


@app.get("/brain/stats")
async def brain_stats() -> dict[str, Any]:
    return {"ok": True, **brain.stats()}


@app.get("/brain/trace/recent")
async def brain_trace_recent(n: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return {"ok": True, "items": brain.recent_traces(n)}


if __name__ == "__main__":
    uvicorn.run(app, host=cfg.host, port=cfg.port)
