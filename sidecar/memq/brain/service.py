from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import MemqConfig
from ..db import MemqDB
from ..structured_facts import normalize_fact_value, plausible_fact_value, structured_fact_summary
from ..style import sanitize_style_profile
from .ollama_client import BrainUnavailable, OllamaBrainClient, OllamaConfig
from .schemas import BrainAuditPatchPlan, BrainIngestPlan, BrainMergePlan, BrainRecallPlan


SAFE_FACT_KEY_PREFIX = ("profile.", "pref.", "policy.", "project.", "relationship.", "timeline.", "rule.", "memory.")
SAFE_RULE_PREFIX = ("language.", "security.", "procedure.", "compliance.", "output.", "operation.", "identity.")
SAFE_STYLE_KEYS = {"tone", "persona", "verbosity", "firstPerson", "callUser", "prefix", "speakingStyle", "avoid"}
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{10,}|BEGIN (?:RSA|OPENSSH|PRIVATE) KEY|eyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})",
    re.IGNORECASE,
)


@dataclass
class BrainResult:
    used: bool
    reason: str = ""


class BrainService:
    def __init__(self, cfg: MemqConfig) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.brain_enabled and cfg.brain_provider.lower() == "ollama")
        self.required = str(cfg.brain_mode or "best_effort").lower() == "required"
        self.mode = str(cfg.brain_mode or "best_effort").lower()
        if self.mode == "off":
            self.enabled = False
        self.provider = str(cfg.brain_provider or "disabled")
        self.model = str(cfg.brain_model or "")
        self.client: Optional[OllamaBrainClient] = None
        self._retry_after_sec = max(5, min(20, int(max(1000, cfg.brain_timeout_ms) / 1000)))
        if self.enabled:
            self.client = OllamaBrainClient(
                OllamaConfig(
                    base_url=cfg.brain_base_url,
                    model=cfg.brain_model,
                    timeout_ms=cfg.brain_timeout_ms,
                    keep_alive=cfg.brain_keep_alive,
                    temperature=cfg.brain_temperature,
                    max_tokens=cfg.brain_max_tokens,
                    concurrent=cfg.brain_concurrent,
                    required_mode=self.required,
                    auto_restart=cfg.brain_auto_restart,
                    restart_cooldown_sec=cfg.brain_restart_cooldown_sec,
                    restart_wait_ms=cfg.brain_restart_wait_ms,
                )
            )
        self.trace_path = (cfg.db_path.parent / "brain_trace.jsonl").resolve()
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_error: str = ""
        self._last_error_type: str = ""
        self._last_error_ts: int = 0
        self._last_trace_id: str = ""
        self._last_trace_by_op: Dict[str, str] = {}
        self._last_ps_seen_model: str = ""
        self._last_ps_seen_ts: int = 0
        self._stats: Dict[str, Any] = {
            "total_calls": 0,
            "ok_calls": 0,
            "err_calls": 0,
            "ops": {},
        }

    def _compact_text(self, text: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        return " ".join(str(text or "").split()).strip()[:max_chars]

    def _compact_recent_messages(self, recent_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        max_items = max(1, int(self.cfg.brain_recall_recent_messages))
        max_chars = max(80, int(self.cfg.brain_recall_message_chars))
        for m in (recent_messages or [])[-max_items:]:
            role = str((m or {}).get("role") or "")
            text = self._compact_text(str((m or {}).get("text") or ""), max_chars)
            if not text:
                continue
            ts = (m or {}).get("ts")
            out.append({"role": role, "text": text, "ts": ts})
        return out

    def _can_attempt(self) -> bool:
        if self.required:
            return True
        if not self._last_error_ts:
            return True
        return (int(time.time()) - int(self._last_error_ts)) >= int(self._retry_after_sec)

    def _mark_error(self, err: str, *, err_type: str = "brain_error", trace_id: str = "") -> None:
        self._last_error = str(err or "brain_error")
        self._last_error_type = str(err_type or "brain_error")
        self._last_error_ts = int(time.time())
        if trace_id:
            self._last_trace_id = trace_id

    def _append_trace(self, row: Dict[str, Any]) -> None:
        try:
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _record_call_stats(self, op: str, *, ok: bool, trace_id: str) -> None:
        self._stats["total_calls"] = int(self._stats.get("total_calls", 0)) + 1
        if ok:
            self._stats["ok_calls"] = int(self._stats.get("ok_calls", 0)) + 1
            self._stats["last_ok_ts"] = int(time.time())
        else:
            self._stats["err_calls"] = int(self._stats.get("err_calls", 0)) + 1
            self._stats["last_err_ts"] = int(time.time())
        self._stats["last_trace_id"] = trace_id
        ops = self._stats.setdefault("ops", {})
        o = ops.setdefault(op, {"total": 0, "ok": 0, "err": 0})
        o["total"] = int(o.get("total", 0)) + 1
        if ok:
            o["ok"] = int(o.get("ok", 0)) + 1
        else:
            o["err"] = int(o.get("err", 0)) + 1

    def _proof_ps(self) -> Dict[str, Any]:
        if not self.client:
            raise BrainUnavailable("brain_client_missing")
        ps = self.client.get_ps_snapshot()
        if bool(ps.get("seen")):
            self._last_ps_seen_model = self.model
            self._last_ps_seen_ts = int(time.time())
        return ps

    def _call_plan(
        self,
        *,
        op: str,
        session_key: str,
        payload: Dict[str, Any],
        call: Callable[[], Any],
        schema_version: str = "memq_brain_v1",
    ) -> Optional[Any]:
        trace_id = str(uuid.uuid4())
        t0 = int(time.time() * 1000)
        self._last_trace_by_op[op] = trace_id
        self._last_trace_id = trace_id
        prompt_sha = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="ignore")
        ).hexdigest()

        if not self.client:
            err = "brain_disabled_or_no_client"
            self._mark_error(err, err_type="brain_unavailable", trace_id=trace_id)
            self._record_call_stats(op, ok=False, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": op,
                    "provider": self.provider,
                    "model": self.model,
                    "ok": False,
                    "latency_ms": max(0, int(time.time() * 1000) - t0),
                    "prompt_sha256": prompt_sha,
                    "schema_version": schema_version,
                    "err_type": "brain_unavailable",
                    "err_msg": err,
                    "ps_snapshot": {"ok": False, "seen": False, "matched": None},
                }
            )
            if self.required:
                raise BrainUnavailable(err)
            return None

        if not self._can_attempt():
            err = "brain_cooldown"
            self._mark_error(err, err_type="brain_cooldown", trace_id=trace_id)
            self._record_call_stats(op, ok=False, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": op,
                    "provider": self.provider,
                    "model": self.model,
                    "ok": False,
                    "latency_ms": max(0, int(time.time() * 1000) - t0),
                    "prompt_sha256": prompt_sha,
                    "schema_version": schema_version,
                    "err_type": "brain_cooldown",
                    "err_msg": err,
                    "ps_snapshot": {"ok": False, "seen": False, "matched": None},
                }
            )
            if self.required:
                raise BrainUnavailable(err)
            return None

        try:
            out = call()
            ps = self._proof_ps()
            if self.required and not bool(ps.get("seen")):
                raise BrainUnavailable("brain_proof_failed")
            latency_ms = max(0, int(time.time() * 1000) - t0)
            self._record_call_stats(op, ok=True, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": op,
                    "provider": self.provider,
                    "model": self.model,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "prompt_sha256": prompt_sha,
                    "schema_version": schema_version,
                    "ollama_response_stats": self.client.last_chat_stats,
                    "ps_snapshot": ps,
                }
            )
            return out
        except BrainUnavailable as e:
            msg = str(e) or "brain_unavailable"
            self._mark_error(msg, err_type=type(e).__name__, trace_id=trace_id)
            latency_ms = max(0, int(time.time() * 1000) - t0)
            self._record_call_stats(op, ok=False, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": op,
                    "provider": self.provider,
                    "model": self.model,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "prompt_sha256": prompt_sha,
                    "schema_version": schema_version,
                    "err_type": type(e).__name__,
                    "err_msg": msg,
                    "ollama_response_stats": self.client.last_chat_stats if self.client else {},
                    "ps_snapshot": {"ok": False, "seen": False, "matched": None},
                }
            )
            if self.required:
                raise
            return None
        except Exception as e:
            msg = str(e) or "brain_unknown_error"
            self._mark_error(msg, err_type=type(e).__name__, trace_id=trace_id)
            latency_ms = max(0, int(time.time() * 1000) - t0)
            self._record_call_stats(op, ok=False, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": op,
                    "provider": self.provider,
                    "model": self.model,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "prompt_sha256": prompt_sha,
                    "schema_version": schema_version,
                    "err_type": type(e).__name__,
                    "err_msg": msg,
                    "ps_snapshot": {"ok": False, "seen": False, "matched": None},
                }
            )
            if self.required:
                raise BrainUnavailable(msg) from e
            return None

    def last_trace_id(self, op: str) -> str:
        return str(self._last_trace_by_op.get(op) or "")

    def record_apply(self, *, op: str, session_key: str, trace_id: str, apply_summary: Dict[str, Any]) -> None:
        if not trace_id:
            trace_id = str(uuid.uuid4())
        self._append_trace(
            {
                "ts": int(time.time()),
                "trace_id": trace_id,
                "session_key": session_key,
                "op": f"{op}_apply",
                "provider": self.provider,
                "model": self.model,
                "ok": True,
                "latency_ms": 0,
                "prompt_sha256": "",
                "schema_version": "memq_brain_v1",
                "apply_summary": dict(apply_summary or {}),
                "ps_snapshot": {"ok": True, "seen": bool(self._last_ps_seen_model), "matched": {"model": self._last_ps_seen_model or self.model}},
            }
        )

    def recent_traces(self, n: int = 50) -> List[Dict[str, Any]]:
        n = max(1, min(500, int(n)))
        if not self.trace_path.exists():
            return []
        try:
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for ln in lines[-n:]:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            **self._stats,
            "enabled": self.enabled,
            "required": self.required,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "last_error": self._last_error,
            "last_error_type": self._last_error_type,
            "last_error_ts": self._last_error_ts,
            "last_trace_id": self._last_trace_id,
            "last_ps_seen_model": self._last_ps_seen_model,
            "last_ps_seen_ts": self._last_ps_seen_ts,
            "trace_path": str(self.trace_path),
        }

    @property
    def status(self) -> Dict[str, Any]:
        next_retry_at = 0
        if self._last_error_ts and not self.required:
            next_retry_at = int(self._last_error_ts) + int(self._retry_after_sec)
        return {
            "enabled": self.enabled,
            "required": self.required,
            "mode": self.mode,
            "provider": "ollama" if self.enabled else "disabled",
            "model": self.model,
            "last_error": self._last_error,
            "last_error_ts": self._last_error_ts,
            "next_retry_at": next_retry_at,
            "last_trace_id": self._last_trace_id,
            "last_ps_seen_model": self._last_ps_seen_model,
            "last_ps_seen_ts": self._last_ps_seen_ts,
        }

    def build_ingest_plan(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        ts: int,
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[BrainIngestPlan]:
        user_compact = self._compact_text(user_text, int(self.cfg.brain_ingest_user_chars))
        assistant_compact = self._compact_text(assistant_text, int(self.cfg.brain_ingest_assistant_chars))
        md = dict(metadata or {})
        # Keep metadata compact to avoid bloating local 20B prompts.
        if "actions" in md and isinstance(md.get("actions"), list):
            md["actions"] = list(md.get("actions") or [])[:6]
        if "actionSummaries" in md and isinstance(md.get("actionSummaries"), list):
            md["actionSummaries"] = [self._compact_text(str(x), 120) for x in list(md.get("actionSummaries") or [])[:8]]
        payload = {
            "session_key": session_key,
            "ts": int(ts),
            "user_text": user_compact,
            "assistant_text": assistant_compact,
            "metadata": md,
        }
        return self._call_plan(
            op="ingest_plan",
            session_key=session_key,
            payload=payload,
            call=lambda: self.client.build_ingest_plan(
                session_key=session_key,
                user_text=user_compact,
                assistant_text=assistant_compact,
                ts=ts,
                metadata=md,
            ) if self.client else None,
        )

    def ensure_runtime(self, *, session_key: str = "runtime") -> Dict[str, Any]:
        trace_id = str(uuid.uuid4())
        if not self.client:
            self._mark_error("brain_disabled_or_no_client", err_type="brain_unavailable", trace_id=trace_id)
            return {"ok": False, "seen": False, "trace_id": trace_id, "err": "brain_disabled_or_no_client"}
        try:
            ps = self.client.ensure_runtime()
            if bool(ps.get("seen")):
                self._last_ps_seen_model = self.model
                self._last_ps_seen_ts = int(time.time())
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": "runtime_ensure",
                    "provider": self.provider,
                    "model": self.model,
                    "ok": bool(ps.get("seen")),
                    "latency_ms": 0,
                    "prompt_sha256": "",
                    "schema_version": "memq_brain_v1",
                    "ps_snapshot": ps,
                }
            )
            return {"ok": True, "seen": bool(ps.get("seen")), "trace_id": trace_id, "ps": ps}
        except Exception as e:
            self._mark_error(str(e), err_type=type(e).__name__, trace_id=trace_id)
            self._append_trace(
                {
                    "ts": int(time.time()),
                    "trace_id": trace_id,
                    "session_key": session_key,
                    "op": "runtime_ensure",
                    "provider": self.provider,
                    "model": self.model,
                    "ok": False,
                    "latency_ms": 0,
                    "prompt_sha256": "",
                    "schema_version": "memq_brain_v1",
                    "err_type": type(e).__name__,
                    "err_msg": str(e),
                    "ps_snapshot": {"ok": False, "seen": False, "matched": None},
                }
            )
            if self.required:
                raise BrainUnavailable(str(e)) from e
            return {"ok": False, "seen": False, "trace_id": trace_id, "err": str(e)}

    def build_recall_plan(
        self,
        *,
        session_key: str,
        prompt: str,
        recent_messages: List[Dict[str, Any]],
        budgets: Dict[str, int],
        top_k: int,
        surface_threshold: float,
        deep_enabled: bool,
    ) -> Optional[BrainRecallPlan]:
        prompt_compact = self._compact_text(prompt, 320)
        recent_compact = self._compact_recent_messages(recent_messages)
        payload = {
            "session_key": session_key,
            "prompt": prompt_compact,
            "recent_messages": recent_compact,
            "budgets": budgets,
            "top_k": int(top_k),
            "surface_threshold": float(surface_threshold),
            "deep_enabled": bool(deep_enabled),
        }
        return self._call_plan(
            op="recall_plan",
            session_key=session_key,
            payload=payload,
            call=lambda: self.client.build_recall_plan(
                session_key=session_key,
                prompt=prompt_compact,
                recent_messages=recent_compact,
                budgets=budgets,
                top_k=top_k,
                surface_threshold=surface_threshold,
                deep_enabled=deep_enabled,
            ) if self.client else None,
        )

    def build_merge_plan(
        self,
        *,
        session_key: str,
        memory_candidates: List[Dict[str, Any]],
        stats: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrainMergePlan]:
        merge_limit = max(20, int(self.cfg.brain_merge_candidate_limit))
        compact_candidates: List[Dict[str, Any]] = []
        for r in (memory_candidates or [])[:merge_limit]:
            compact_candidates.append(
                {
                    "id": str((r or {}).get("id") or ""),
                    "session_key": str((r or {}).get("session_key") or ""),
                    "layer": str((r or {}).get("layer") or ""),
                    "summary": self._compact_text(str((r or {}).get("summary") or ""), 220),
                    "updated_at": int((r or {}).get("updated_at") or 0),
                    "importance": float((r or {}).get("importance") or 0.0),
                    "usage_count": int((r or {}).get("usage_count") or 0),
                }
            )
        payload = {
            "session_key": session_key,
            "memory_candidates": compact_candidates,
            "stats": stats or {},
        }
        return self._call_plan(
            op="merge_plan",
            session_key=session_key,
            payload=payload,
            call=lambda: self.client.build_merge_plan(
                session_key=session_key,
                memory_candidates=compact_candidates,
                stats=stats or {},
            ) if self.client else None,
        )

    def build_audit_patch_plan(
        self,
        *,
        text: str,
        allowed_languages: List[str],
        reasons: Optional[List[str]] = None,
    ) -> Optional[BrainAuditPatchPlan]:
        payload = {
            "text": text,
            "allowed_languages": allowed_languages,
            "reasons": reasons or [],
        }
        return self._call_plan(
            op="audit_patch_plan",
            session_key="audit",
            payload=payload,
            call=lambda: self.client.build_audit_patch_plan(
                text=text,
                allowed_languages=allowed_languages,
                reasons=reasons or [],
            ) if self.client else None,
        )

    def apply_merge_plan(self, *, db: MemqDB, session_key: str, plan: BrainMergePlan) -> Dict[str, int]:
        now = int(time.time())
        stats = {"merged": 0, "pruned": 0, "quarantined": 0}

        for p in plan.prunes or []:
            rid = str(p.id or "").strip()
            if not rid:
                continue
            rc = db.conn.execute("DELETE FROM memory_items WHERE id=?", (rid,)).rowcount
            stats["pruned"] += int(rc or 0)

        for m in plan.merges or []:
            target_id = str(m.target_id or "").strip()
            if not target_id:
                continue
            row = db.conn.execute("SELECT * FROM memory_items WHERE id=?", (target_id,)).fetchone()
            if not row:
                continue
            text = " ".join(str(m.merged_text or row["text"] or "").split()).strip()[:1200]
            summary = " ".join(str(m.merged_summary or row["summary"] or text).split()).strip()[:420]
            try:
                tags = json.loads(str(row["tags"] or "{}"))
            except Exception:
                tags = {}
            if not isinstance(tags, dict):
                tags = {}
            new_tags = m.new_tags or {}
            for k, v in new_tags.items():
                tags[str(k)] = str(v)

            quarantine_flag = str(new_tags.get("quarantine") or "").strip().lower() in {"1", "true", "yes", "on"}
            if quarantine_flag:
                db.add_quarantine(
                    trace_id=target_id,
                    raw_text=summary[:500],
                    reason=str(new_tags.get("reason") or "merge_quarantine")[:80],
                    risk_score=0.8,
                )
                stats["quarantined"] += 1
                db.conn.execute("DELETE FROM memory_items WHERE id=?", (target_id,))
            else:
                db.conn.execute(
                    "UPDATE memory_items SET text=?,summary=?,tags=?,updated_at=? WHERE id=?",
                    (text, summary, json.dumps(tags, ensure_ascii=False), now, target_id),
                )
                stats["merged"] += 1

            source_ids = [str(x).strip() for x in (m.source_ids or []) if str(x).strip() and str(x).strip() != target_id]
            if bool(m.drop_source) and source_ids:
                for sid in source_ids:
                    db.conn.execute("DELETE FROM memory_items WHERE id=?", (sid,))
                    stats["pruned"] += 1

        db.conn.commit()
        # Re-sync derived indices after merge application.
        db.backfill_fact_keys(layer="deep", limit=30000)
        db.backfill_fact_index(layer="deep", limit=30000)
        db.cleanup_stale_fact_index()
        return stats

    def apply_ingest_plan(
        self,
        *,
        db: MemqDB,
        session_key: str,
        ts: int,
        plan: BrainIngestPlan,
        user_text: str,
        assistant_text: str,
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, int]:
        wrote = {"surface": 0, "deep": 0, "ephemeral": 0, "quarantined": 0, "events": 0}
        now = int(ts or time.time())

        def quarantine(raw: str, reason: str, risk: float = 0.8) -> None:
            nonlocal wrote
            db.add_quarantine(None, raw_text=raw[:500], reason=reason[:80], risk_score=max(0.0, min(1.0, risk)))
            wrote["quarantined"] += 1

        for q in plan.quarantine or []:
            raw = str(q.raw_snippet or "")
            if raw:
                quarantine(raw, str(q.reason or "brain_quarantine"), 0.75)

        for fact in plan.facts or []:
            fk = str(fact.fact_key or "").strip().lower()
            if not fk or not fk.startswith(SAFE_FACT_KEY_PREFIX):
                quarantine(str(fact.value or ""), "unknown_fact_key", 0.7)
                continue
            val = normalize_fact_value(str(fact.value or ""), max_len=96)
            if not val:
                continue
            if SECRET_RE.search(val):
                quarantine(val, "secret_like_fact", 0.95)
                continue
            if not plausible_fact_value(fk, val):
                quarantine(val, "implausible_fact_value", 0.65)
                continue
            layer = str(fact.layer or "surface")
            if layer not in {"surface", "deep", "ephemeral"}:
                layer = "surface"
            conf = float(fact.confidence)
            if layer == "deep" and conf < 0.45:
                layer = "surface"
            ttl_days = max(1, int(fact.ttl_days or (365 if layer == "deep" else 14)))
            ttl_expires_at = now + ttl_days * 86400
            if layer == "deep" and ttl_days >= 365 and conf >= 0.75:
                ttl_expires_at = None
            summary_fact = {
                "subject": str(fact.entity_id or "ent:user"),
                "relation": fk.replace(".", "_"),
                "value": val,
                "fact_key": fk,
                "confidence": conf,
                "source": "brain_ingest",
                "stable": layer == "deep",
                "ttl_days": ttl_days,
                "explicit": False,
                "ts": now,
            }
            summary = structured_fact_summary(summary_fact)
            item_id = db.add_memory_item(
                session_key=session_key,
                layer=layer,
                text=summary,
                summary=summary,
                importance=max(0.45, min(0.95, conf)),
                tags={
                    "kind": "structured_fact",
                    "from": "brain_ingest",
                    "ts": now,
                    "fact_keys": [fk],
                    "fact": summary_fact,
                    "keywords": list(fact.keywords or []),
                    "evidence_quote": str(fact.evidence_quote or "")[:120],
                },
                emb_f16=None,
                emb_q=None,
                emb_dim=0,
                ttl_expires_at=ttl_expires_at,
                source="brain_ingest",
            )
            wrote[layer] += 1
            if layer == "deep":
                db.expire_conflicting_fact_keys("deep", session_key, [fk], item_id)
                if fk.startswith("profile.") and conf >= 0.75:
                    gid = db.add_memory_item(
                        session_key="global",
                        layer="deep",
                        text=summary,
                        summary=summary,
                        importance=max(0.6, min(0.96, conf)),
                        tags={
                            "kind": "durable_global_fact",
                            "from": "brain_ingest",
                            "ts": now,
                            "fact_keys": [fk],
                            "fact": summary_fact,
                            "keywords": list(fact.keywords or []),
                        },
                        emb_f16=None,
                        emb_q=None,
                        emb_dim=0,
                        ttl_expires_at=None,
                        source="brain_ingest",
                    )
                    db.expire_conflicting_fact_keys("deep", "global", [fk], gid)

        event_written = 0
        for ev in plan.events or []:
            sm = " ".join(str(ev.summary or "").split()).strip()
            if not sm:
                continue
            if SECRET_RE.search(sm):
                quarantine(sm, "secret_like_event", 0.9)
                continue
            ttl_days = max(1, int(ev.ttl_days or (30 if float(ev.salience) >= 0.7 else 14)))
            if float(ev.salience) >= 0.9:
                ttl = None
            else:
                ttl = now + ttl_days * 86400
            db.add_event(
                session_key=session_key,
                ts=int(ev.ts or now),
                actor=str(ev.actor or "assistant"),
                kind=str(ev.kind or "chat"),
                summary=sm[:260],
                tags={"from": "brain_ingest", "keywords": list(ev.keywords or [])},
                importance=float(ev.salience),
                ttl_expires_at=ttl,
            )
            event_written += 1

        if event_written == 0:
            if user_text.strip():
                db.add_event(
                    session_key=session_key,
                    ts=now,
                    actor="user",
                    kind="chat",
                    summary=" ".join(user_text.split())[:260],
                    tags={"from": "brain_fallback"},
                    importance=0.4,
                    ttl_expires_at=now + 14 * 86400,
                )
                event_written += 1
            if assistant_text.strip():
                db.add_event(
                    session_key=session_key,
                    ts=now,
                    actor="assistant",
                    kind="chat",
                    summary=" ".join(assistant_text.split())[:260],
                    tags={"from": "brain_fallback"},
                    importance=0.35,
                    ttl_expires_at=now + 14 * 86400,
                )
                event_written += 1
            for a in ((metadata or {}).get("actions") or []):
                sm = " ".join(str((a or {}).get("summary") or "").split()).strip()
                if not sm:
                    continue
                db.add_event(
                    session_key=session_key,
                    ts=now,
                    actor="assistant",
                    kind=str((a or {}).get("kind") or "action"),
                    summary=sm[:260],
                    tags={"from": "brain_fallback"},
                    importance=0.65,
                    ttl_expires_at=now + 30 * 86400,
                )
                event_written += 1

        wrote["events"] += event_written

        su = plan.style_update
        if su and su.apply and su.explicit:
            for k, v in (su.keys or {}).items():
                kk = str(k or "").strip()
                if kk not in SAFE_STYLE_KEYS:
                    continue
                vv = " ".join(str(v or "").split()).strip()
                if not vv or SECRET_RE.search(vv):
                    continue
                db.upsert_style(kk, vv[:180])
            sanitize_style_profile(db)

        ru = plan.rules_update
        if ru and ru.apply and ru.explicit:
            for i, line in enumerate(ru.rules or []):
                t = " ".join(str(line or "").split()).strip()
                if "=" not in t:
                    continue
                k, v = t.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k or not k.startswith(SAFE_RULE_PREFIX):
                    continue
                if SECRET_RE.search(v):
                    continue
                rid = f"brain_{k}_{i}".replace(" ", "_").replace("/", "_")
                kind = k.split(".", 1)[0]
                db.upsert_rule(rid, priority=75, enabled=True, kind=kind, body=f"{k}={v[:200]}")

        return wrote
