from __future__ import annotations

from datetime import datetime
import contextlib
from pathlib import Path
import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, Field
import uvicorn

from sidecar.memq.brain.schemas import BrainIngestPlan
from sidecar.memq.brain.service import BrainService, explicit_rule_requested, explicit_style_requested
from sidecar.memq.config import Config, load_config
from sidecar.memq.db import MemqDB
from sidecar.memq.lancedb_bridge import LanceDbMemoryBackend
from sidecar.memq.local_overrides import load_local_overrides, write_current_snapshots
from sidecar.memq.memory_source import list_qrule, list_qstyle, profile_snapshot, recent_brain_context
from sidecar.memq.prompt_blueprint import (
    BrainPlanningError,
    PromptBlueprintBudgets,
    PromptBlueprintRequest,
    build_prompt_blueprint,
)


class Message(BaseModel):
    role: str
    text: str
    ts: int | None = None


class QueryBudgets(BaseModel):
    qctxTokens: int = Field(validation_alias=AliasChoices("qctxTokens", "memctxTokens"))
    qruleTokens: int = Field(validation_alias=AliasChoices("qruleTokens", "rulesTokens"))
    qstyleTokens: int = Field(validation_alias=AliasChoices("qstyleTokens", "styleTokens"))


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


class PreviewRequest(BaseModel):
    sessionKey: str
    userText: str
    ts: int | None = None


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
memory_backend = LanceDbMemoryBackend(cfg.lancedb_path, cfg.lancedb_helper)
app = FastAPI(title="Hybrid MEMQ v3")
last_activity_at = 0
idle_task: asyncio.Task[Any] | None = None
idle_failure = ""


def _use_memory_backend() -> bool:
    return cfg.qctx_backend == "memory-lancedb-pro"


def _effective_profile_snapshot(snapshot: str, style: dict[str, str]) -> str:
    ordered = ["callUser", "firstPerson", "persona", "tone", "speaking_style", "verbosity"]
    style_parts: dict[str, str] = {}
    for key in ordered:
        value = str(style.get(key) or "").strip()
        if value:
            style_parts[key] = value
    extra_parts: list[str] = []
    for segment in str(snapshot or "").split("|"):
        clean = " ".join(segment.split()).strip()
        if not clean or ":" not in clean:
            continue
        key = clean.split(":", 1)[0].strip()
        if key in ordered:
            continue
        if clean not in extra_parts:
            extra_parts.append(clean)
    parts = [f"{key}:{style_parts[key]}" for key in ordered if key in style_parts]
    parts.extend(extra_parts)
    merged = " | ".join(parts[:8])
    return (
        merged.replace("MEMRULES", "QRULE")
        .replace("MEMRULE", "QRULE")
        .replace("MEMSTYLE", "QSTYLE")
        .replace("MEMCTX", "QCTX")
    )


def _effective_profile_snapshot_for_api(db: MemqDB, session_key: str, style: dict[str, str]) -> str:
    return db.compute_public_profile_snapshot(session_key, style)


def _rewrite_public_labels(text: str) -> str:
    return (
        str(text or "")
        .replace("MEMRULES", "QRULE")
        .replace("MEMRULE", "QRULE")
        .replace("MEMSTYLE", "QSTYLE")
        .replace("MEMCTX", "QCTX")
    )


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
    global idle_failure
    while True:
        await asyncio.sleep(3600)
        idle_failure = ""


@app.on_event("startup")
async def startup() -> None:
    global idle_task
    idle_task = None


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
    overrides = load_local_overrides(cfg.root)
    return {
        "ok": True,
        "config": {
            "memoryBackend": cfg.qctx_backend,
            "qctxBackend": cfg.qctx_backend,
            "timezone": cfg.timezone,
            "budgets": {
                "qctx": cfg.budgets.qctx_tokens,
                "qrule": cfg.budgets.qrule_tokens,
                "qstyle": cfg.budgets.qstyle_tokens,
            },
            "overrides": {
                "qstylePath": str(overrides.qstyle_path),
                "qrulePath": str(overrides.qrule_path),
                "qstyleKeys": sorted(overrides.qstyle.keys()),
                "qruleKeys": sorted(overrides.qrule.keys()),
            },
        },
        "qbrain": {
            "mode": cfg.brain.mode,
            "model": cfg.brain.model,
            "provider": cfg.brain.provider,
        },
        "brain": brain.stats(),
        "db": str(cfg.db_path),
        "lancedb": {
            "path": str(cfg.lancedb_path),
            "helper": str(cfg.lancedb_helper),
            "enabled": memory_backend.enabled(),
            "implementation": "memory-lancedb-pro-adapted" if memory_backend.enabled() else "disabled",
        },
        "idle": {
            "enabled": cfg.idle_enabled,
            "backgroundEnabled": cfg.idle_background_enabled,
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
        if memory_backend.enabled():
            memory_backend.ingest_memories(
                [
                    {
                        "id": f"bootstrap:{name}",
                        "session_key": "global",
                        "layer": "deep",
                        "kind": "carry",
                        "fact_key": fact_key,
                        "value": text[:240],
                        "text": text,
                        "summary": text[:240],
                        "importance": 0.9,
                        "confidence": 0.9,
                        "strength": 0.9,
                        "timestamp": int(datetime.now().timestamp()),
                    }
                ]
            )
        imported += 1
    return {"ok": True, "imported": imported}


@app.post("/memory/ingest_turn")
async def memory_ingest_turn(req: IngestRequest) -> dict[str, Any]:
    global last_activity_at
    last_activity_at = req.ts
    overrides = load_local_overrides(cfg.root)
    style = {**list_qstyle(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qstyle}
    rules = {**list_qrule(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qrule}
    recent_summary = recent_brain_context(db, memory_backend if _use_memory_backend() else None, req.sessionKey)
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
    wrote = brain.apply_ingest_plan(
        db,
        session_key=req.sessionKey,
        plan=plan,
        ts=req.ts,
        user_text=req.userText,
        memory_backend=memory_backend if _use_memory_backend() else None,
    )
    return {"ok": True, "wrote": wrote, "traceId": trace_id}


@app.post("/memory/preview_prompt")
async def memory_preview_prompt(req: PreviewRequest) -> dict[str, Any]:
    now_ts = req.ts or int(datetime.now().timestamp())
    if not explicit_style_requested(req.userText) and not explicit_rule_requested(req.userText):
        return {"ok": True, "applied": False, "wrote": {"facts": 0, "events": 0, "style": 0, "rules": 0, "quarantine": 0}, "traceId": ""}
    overrides = load_local_overrides(cfg.root)
    style = {**list_qstyle(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qstyle}
    rules = {**list_qrule(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qrule}
    recent_summary = recent_brain_context(db, memory_backend if _use_memory_backend() else None, req.sessionKey)
    try:
        plan, trace_id, _ = await brain.build_ingest_plan(
            session_key=req.sessionKey,
            user_text=req.userText,
            assistant_text="",
            current_style=style,
            current_rules=rules,
            recent_summary=recent_summary,
        )
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="preview_ingest_plan", session_key=req.sessionKey)
        return {"ok": True, "wrote": {"facts": 0, "events": 0, "style": 0, "rules": 0, "quarantine": 0}, "traceId": ""}
    wrote = brain.apply_ingest_plan(
        db,
        session_key=req.sessionKey,
        plan=plan,
        ts=now_ts,
        user_text=req.userText,
        style_rules_only=True,
        memory_backend=memory_backend if _use_memory_backend() else None,
    )
    overrides_after = load_local_overrides(cfg.root)
    effective_qstyle = {**list_qstyle(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides_after.qstyle}
    effective_qrule = {**list_qrule(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides_after.qrule}
    write_current_snapshots(cfg.root, qstyle=effective_qstyle, qrule=effective_qrule)
    return {"ok": True, "applied": True, "wrote": wrote, "traceId": trace_id}


@app.post("/conversation/summarize")
async def conversation_summarize(req: SummarizeRequest) -> dict[str, Any]:
    joined = "\n".join(f"{m.role}:{m.text}" for m in req.prunedMessages[-8:])
    if not joined.strip():
        return {"ok": True, "summary": ""}
    overrides = load_local_overrides(cfg.root)
    try:
        plan, trace_id, _ = await brain.build_ingest_plan(
            session_key=req.sessionKey,
            user_text=joined,
            assistant_text="",
            current_style={**list_qstyle(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qstyle},
            current_rules={**list_qrule(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides.qrule},
            recent_summary=recent_brain_context(db, memory_backend if _use_memory_backend() else None, req.sessionKey),
        )
    except Exception as exc:
        if cfg.brain_required:
            await _raise_brain(exc, code="brain_unavailable", op="conversation_summarize", session_key=req.sessionKey)
        raise
    wrote = brain.apply_ingest_plan(
        db,
        session_key=req.sessionKey,
        plan=plan,
        ts=int(datetime.now().timestamp()),
        user_text=joined,
        memory_backend=memory_backend if _use_memory_backend() else None,
    )
    return {"ok": True, "traceId": trace_id, "wrote": wrote}


async def _qctx_query_impl(req: QueryRequest) -> dict[str, Any]:
    global last_activity_at
    last_activity_at = int(datetime.now().timestamp())
    try:
        blueprint = await build_prompt_blueprint(
            cfg=cfg,
            db=db,
            brain=brain,
            memory_backend=memory_backend if _use_memory_backend() else None,
            request=PromptBlueprintRequest(
                session_key=req.sessionKey,
                prompt=req.prompt,
                recent_messages=[m.model_dump() for m in req.recentMessages],
                budgets=PromptBlueprintBudgets(
                    qctx_tokens=req.budgets.qctxTokens,
                    qrule_tokens=req.budgets.qruleTokens,
                    qstyle_tokens=req.budgets.qstyleTokens,
                ),
                top_k=req.topK,
                now_iso=datetime.now().astimezone().isoformat(),
            ),
        )
    except BrainPlanningError as exc:
        await _raise_brain(exc.original, code="brain_unavailable", op="recall_plan", session_key=req.sessionKey)
    response = blueprint.to_response()
    response["qrule"] = _rewrite_public_labels(response.get("qrule", ""))
    response["qstyle"] = _rewrite_public_labels(response.get("qstyle", ""))
    response["qctx"] = _rewrite_public_labels(response.get("qctx", ""))
    try:
        overrides_now = load_local_overrides(cfg.root)
        effective_qstyle = {**list_qstyle(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides_now.qstyle}
        effective_qrule = {**list_qrule(db, memory_backend if _use_memory_backend() else None, req.sessionKey), **overrides_now.qrule}
        write_current_snapshots(
            cfg.root,
            qstyle=effective_qstyle,
            qrule=effective_qrule,
            qctx=response.get("qctx", ""),
        )
    except Exception:
        pass
    return response


@app.post("/qctx/query")
async def qctx_query(req: QueryRequest) -> dict[str, Any]:
    return await _qctx_query_impl(req)


@app.post("/memctx/query")
async def qctx_query_compat(req: QueryRequest) -> dict[str, Any]:
    return await _qctx_query_impl(req)


@app.post("/idle/run_once")
async def idle_run_once(req: IdleRequest) -> dict[str, Any]:
    return {
        "ok": True,
        "did": ["disabled"],
        "stats": {"did": ["disabled"], "reason": "sleep_consolidation_disabled"},
        "traceId": None,
        "psSeen": 0,
    }


@app.post("/audit/output")
async def audit_output_endpoint(req: AuditRequest) -> dict[str, Any]:
    return {
        "ok": True,
        "redactedText": req.text,
        "risk": 0.0,
        "block": False,
        "reasons": [],
    }


@app.get("/profile")
async def profile(session_key: str = Query("global")) -> dict[str, Any]:
    overrides = load_local_overrides(cfg.root)
    active_backend = memory_backend if _use_memory_backend() else None
    qstyle = {**list_qstyle(db, active_backend, session_key), **overrides.qstyle}
    qrule = {**list_qrule(db, active_backend, session_key), **overrides.qrule}
    effective_snapshot = profile_snapshot(db, active_backend, session_key, qstyle)
    return {
        "ok": True,
        "qstyle": qstyle,
        "qrule": qrule,
        "qstyleOverride": overrides.qstyle,
        "qruleOverride": overrides.qrule,
        "profile_snapshot": effective_snapshot,
    }


@app.get("/qstyle/current")
async def qstyle_current(session_key: str = Query("global")) -> dict[str, Any]:
    overrides = load_local_overrides(cfg.root)
    active_backend = memory_backend if _use_memory_backend() else None
    qstyle = {**list_qstyle(db, active_backend, session_key), **overrides.qstyle}
    try:
        qrule = {**list_qrule(db, active_backend, session_key), **overrides.qrule}
        write_current_snapshots(cfg.root, qstyle=qstyle, qrule=qrule)
    except Exception:
        pass
    return {"ok": True, "sessionKey": session_key, "qstyle": qstyle, "override": overrides.qstyle}


@app.get("/qrule/current")
async def qrule_current(session_key: str = Query("global")) -> dict[str, Any]:
    overrides = load_local_overrides(cfg.root)
    active_backend = memory_backend if _use_memory_backend() else None
    qrule = {**list_qrule(db, active_backend, session_key), **overrides.qrule}
    try:
        qstyle = {**list_qstyle(db, active_backend, session_key), **overrides.qstyle}
        write_current_snapshots(cfg.root, qstyle=qstyle, qrule=qrule)
    except Exception:
        pass
    return {"ok": True, "sessionKey": session_key, "qrule": qrule, "override": overrides.qrule}


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
