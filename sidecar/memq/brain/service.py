from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
import hashlib
import json
import uuid
from typing import Any

from sidecar.memq.config import Config
from sidecar.memq.db import MemqDB
from sidecar.memq.brain.schemas import (
    BrainAuditPatchPlan,
    BrainIngestPlan,
    BrainMergePlan,
    BrainRecallPlan,
)
from sidecar.memq.brain.ollama_client import BrainUnavailable, OllamaClient, load_prompt


FACT_KEY_PREFIXES = ("profile.", "pref.", "policy.", "project.", "relationship.", "timeline.")
STYLE_KEYS = {"tone", "persona", "verbosity", "speaking_style", "callUser", "firstPerson", "prefix"}
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")


def explicit_style_requested(text: str) -> bool:
    s = str(text or "")
    markers = [
        "口調",
        "話し方",
        "キャラ",
        "人格",
        "一人称",
        "呼び方",
        "呼称",
        "呼んで",
        "として話して",
        "として振る舞",
        "style",
        "persona",
        "tone",
        "speaking style",
        "で話して",
    ]
    return any(marker in s for marker in markers)


def explicit_rule_requested(text: str) -> bool:
    s = str(text or "")
    markers = ["ルール", "今後は必ず", "禁止", "守って", "覚えて", "rule"]
    return any(marker in s for marker in markers)


def _style_key_alias(key: str) -> str | None:
    raw = str(key or "").strip()
    compact = raw.replace("_", "").replace("-", "").replace(" ", "").lower()
    mapping = {
        "tone": "tone",
        "persona": "persona",
        "speakingstyle": "speaking_style",
        "style": "speaking_style",
        "calluser": "callUser",
        "username": "callUser",
        "firstperson": "firstPerson",
        "verbosity": "verbosity",
        "prefix": "prefix",
    }
    if compact in mapping:
        return mapping[compact]
    if raw in STYLE_KEYS:
        return raw
    return None


@dataclass
class BrainCallRecord:
    trace_id: str
    op: str
    ok: bool
    latency_ms: int
    prompt_sha256: str
    stats: dict[str, Any]
    ps_snapshot: dict[str, Any] | None
    apply_summary: dict[str, Any]


class BrainService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = OllamaClient(cfg.brain)
        self.prompt_dir = cfg.root / "sidecar" / "memq" / "brain" / "prompts"
        self.trace_path = cfg.root / ".memq" / "brain_trace.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Semaphore(cfg.brain.concurrency)
        self._stats: dict[str, Any] = {
            "total_calls": 0,
            "ok_calls": 0,
            "err_calls": 0,
            "last_ok_ts": 0,
            "last_err_ts": 0,
            "last_err": "",
            "last_trace_id": "",
            "last_ps_seen_model": "",
        }

    async def close(self) -> None:
        await self.client.close()

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.trace_path.exists():
            return []
        lines = self.trace_path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _write_trace(self, session_key: str, op: str, record: BrainCallRecord) -> None:
        payload = {
            "trace_id": record.trace_id,
            "session_key": session_key,
            "op": op,
            "provider": self.cfg.brain.provider,
            "model": self.cfg.brain.model,
            "ok": record.ok,
            "latency_ms": record.latency_ms,
            "prompt_sha256": record.prompt_sha256,
            "ollama_response_stats": record.stats,
            "ps_snapshot": record.ps_snapshot,
            "apply_summary": record.apply_summary,
        }
        with self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _note_call(self, *, ok: bool, trace_id: str, err: str = "", model_seen: str = "") -> None:
        import time

        now = int(time.time())
        self._stats["total_calls"] += 1
        self._stats["last_trace_id"] = trace_id
        if ok:
            self._stats["ok_calls"] += 1
            self._stats["last_ok_ts"] = now
        else:
            self._stats["err_calls"] += 1
            self._stats["last_err_ts"] = now
            self._stats["last_err"] = err
        if model_seen:
            self._stats["last_ps_seen_model"] = model_seen

    async def _call(self, *, session_key: str, op: str, system_prompt: str, user_prompt: str, schema_model: type[Any]) -> tuple[Any, str, dict[str, Any]]:
        trace_id = str(uuid.uuid4())
        import time

        t0 = time.perf_counter()
        async with self._lock:
            try:
                res = await self.client.chat_schema(system=system_prompt, user=user_prompt, schema_model=schema_model)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                record = BrainCallRecord(
                    trace_id=trace_id,
                    op=op,
                    ok=True,
                    latency_ms=latency_ms,
                    prompt_sha256=res.prompt_sha256,
                    stats=res.stats,
                    ps_snapshot=res.ps_snapshot,
                    apply_summary={},
                )
                self._write_trace(session_key, op, record)
                seen_model = str((res.ps_snapshot or {}).get("model") or (res.ps_snapshot or {}).get("name") or "")
                self._note_call(ok=True, trace_id=trace_id, model_seen=seen_model)
                return schema_model.model_validate(res.content), trace_id, res.stats
            except Exception as exc:  # noqa: BLE001
                latency_ms = int((time.perf_counter() - t0) * 1000)
                prompt_sha = hashlib.sha256(user_prompt.encode("utf-8", "ignore")).hexdigest()
                record = BrainCallRecord(
                    trace_id=trace_id,
                    op=op,
                    ok=False,
                    latency_ms=latency_ms,
                    prompt_sha256=prompt_sha,
                    stats={},
                    ps_snapshot=None,
                    apply_summary={"error": f"{type(exc).__name__}:{exc}"},
                )
                self._write_trace(session_key, op, record)
                self._note_call(ok=False, trace_id=trace_id, err=f"{type(exc).__name__}:{exc}")
                raise

    def _prompt(self, name: str, fallback: str) -> str:
        return load_prompt(self.prompt_dir / name, fallback)

    async def build_ingest_plan(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        current_style: dict[str, str],
        current_rules: dict[str, str],
        recent_summary: str,
    ) -> tuple[BrainIngestPlan, str, dict[str, Any]]:
        system = self._prompt(
            "ingest_system.txt",
            "Return JSON only. Extract memory facts, timeline events, optional explicit style updates, optional explicit rules, and quarantine items. Do not invent facts. Use evidence quotes.",
        )
        user = json.dumps(
            {
                "session_key": session_key,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "current_style": current_style,
                "current_rules": current_rules,
                "recent_summary": recent_summary,
            },
            ensure_ascii=False,
        )
        return await self._call(session_key=session_key, op="ingest_plan", system_prompt=system, user_prompt=user, schema_model=BrainIngestPlan)

    async def build_recall_plan(
        self,
        *,
        session_key: str,
        prompt: str,
        recent_messages: list[dict[str, Any]],
        current_style: dict[str, str],
        current_rules: dict[str, str],
        now_iso: str,
    ) -> tuple[BrainRecallPlan, str, dict[str, Any]]:
        system = self._prompt(
            "recall_system.txt",
            "Return JSON only. Produce intent weights, time range, fact keys, FTS queries, budget split, and retrieval settings for memory recall. Keep queries short and search-oriented.",
        )
        user = json.dumps(
            {
                "session_key": session_key,
                "prompt": prompt,
                "recent_messages": recent_messages[-6:],
                "current_style": current_style,
                "current_rules": current_rules,
                "now": now_iso,
            },
            ensure_ascii=False,
        )
        return await self._call(session_key=session_key, op="recall_plan", system_prompt=system, user_prompt=user, schema_model=BrainRecallPlan)

    async def build_merge_plan(self, *, session_key: str, candidate_groups: list[dict[str, Any]]) -> tuple[BrainMergePlan, str, dict[str, Any]]:
        system = self._prompt(
            "merge_system.txt",
            "Return JSON only. Decide which memory items should merge, keep, or prune. Prefer consolidating duplicates without losing facts.",
        )
        user = json.dumps({"session_key": session_key, "candidate_groups": candidate_groups[:12]}, ensure_ascii=False)
        return await self._call(session_key=session_key, op="merge_plan", system_prompt=system, user_prompt=user, schema_model=BrainMergePlan)

    async def build_audit_patch(self, *, session_key: str, text: str, reasons: list[str]) -> tuple[BrainAuditPatchPlan, str, dict[str, Any]]:
        system = self._prompt(
            "audit_patch_system.txt",
            "Return JSON only. Keep structure, patch only unsafe spans, and preserve meaning.",
        )
        user = json.dumps({"session_key": session_key, "text": text, "reasons": reasons}, ensure_ascii=False)
        return await self._call(session_key=session_key, op="audit_patch", system_prompt=system, user_prompt=user, schema_model=BrainAuditPatchPlan)

    def apply_ingest_plan(self, db: MemqDB, *, session_key: str, plan: BrainIngestPlan, ts: int, user_text: str = "") -> dict[str, int]:
        wrote = {"facts": 0, "events": 0, "style": 0, "rules": 0, "quarantine": 0}
        for item in plan.quarantine:
            db.insert_quarantine(session_key, item.raw_snippet, item.reason, item.risk)
            wrote["quarantine"] += 1
        for fact in plan.facts:
            if not fact.fact_key.startswith(FACT_KEY_PREFIXES):
                db.insert_quarantine(session_key, fact.evidence_quote or fact.value, "unknown_fact_key", 0.8)
                wrote["quarantine"] += 1
                continue
            if fact.layer == "deep" and fact.confidence < 0.45:
                layer = "surface"
            else:
                layer = fact.layer
            db.insert_memory(
                session_key=session_key,
                layer=layer,
                kind="fact",
                fact_key=fact.fact_key,
                value=fact.value,
                text=fact.evidence_quote or fact.value,
                summary=f"{fact.fact_key}:{fact.value}",
                confidence=fact.confidence,
                importance=fact.importance,
                strength=fact.strength,
                tags={"entity_id": fact.entity_id},
                source_quote=fact.evidence_quote,
                ttl_days=fact.ttl_days,
                created_at=ts,
            )
            if layer == "deep" and session_key != "global" and fact.fact_key.startswith("profile."):
                db.insert_memory(
                    session_key="global",
                    layer="deep",
                    kind="fact",
                    fact_key=fact.fact_key,
                    value=fact.value,
                    text=fact.evidence_quote or fact.value,
                    summary=f"{fact.fact_key}:{fact.value}",
                    confidence=fact.confidence,
                    importance=fact.importance,
                    strength=fact.strength,
                    tags={"entity_id": fact.entity_id, "source": session_key},
                    source_quote=fact.evidence_quote,
                    ttl_days=fact.ttl_days,
                    created_at=ts,
                )
            wrote["facts"] += 1
        for event in plan.events:
            db.insert_event(
                session_key=session_key,
                ts=event.ts or ts,
                actor=event.actor,
                kind=event.kind,
                summary=event.summary,
                salience=event.salience,
                keywords=event.keywords,
                ttl_days=event.ttl_days,
            )
            wrote["events"] += 1
        applied_style_keys: set[str] = set()
        if plan.style_update and plan.style_update.apply and plan.style_update.explicit:
            for key, value in plan.style_update.keys.items():
                if key not in STYLE_KEYS:
                    continue
                db.upsert_style(session_key, key, value, updated_at=ts)
                wrote["style"] += 1
                applied_style_keys.add(key)
        if explicit_style_requested(user_text):
            for fact in plan.facts:
                key = _style_key_alias(fact.fact_key)
                if not key or key in applied_style_keys:
                    continue
                db.upsert_style(session_key, key, fact.value, updated_at=ts)
                wrote["style"] += 1
                applied_style_keys.add(key)
        if plan.rules_update and plan.rules_update.apply and plan.rules_update.explicit:
            for key, value in plan.rules_update.rules.items():
                if not key.startswith(RULE_PREFIXES):
                    continue
                db.upsert_rule(session_key, key, value, updated_at=ts)
                wrote["rules"] += 1
        if explicit_rule_requested(user_text):
            for fact in plan.facts:
                if fact.fact_key.startswith(RULE_PREFIXES):
                    db.upsert_rule(session_key, fact.fact_key, fact.value, updated_at=ts)
                    wrote["rules"] += 1
        db.refresh_recent_digests(session_key, days=3)
        return wrote

    def apply_merge_plan(self, db: MemqDB, *, session_key: str, plan: BrainMergePlan) -> dict[str, int]:
        applied = {"merged": 0, "pruned": 0}
        for merge in plan.merges:
            db.apply_merge(merge.target_id, merge.source_ids, merge.merged_summary, merge.merged_value)
            applied["merged"] += 1
        if plan.prunes:
            ids = [item.id for item in plan.prunes]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.conn.execute(f"UPDATE memory_items SET tombstoned=1 WHERE id IN ({placeholders})", ids)
                db.conn.commit()
                applied["pruned"] += len(ids)
        return applied
