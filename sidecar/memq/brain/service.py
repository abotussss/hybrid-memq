from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
        self.enabled = bool(cfg.brain_enabled and cfg.brain_provider.lower() == "ollama")
        self.client: Optional[OllamaBrainClient] = None
        self._retry_after_sec = max(5, min(300, int(cfg.brain_timeout_ms / 1000) * 6))
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
                )
            )
        self._last_error: str = ""
        self._last_error_ts: int = 0

    def _can_attempt(self) -> bool:
        if not self._last_error_ts:
            return True
        return (int(time.time()) - int(self._last_error_ts)) >= int(self._retry_after_sec)

    def _mark_error(self, err: str) -> None:
        self._last_error = str(err or "brain_error")
        self._last_error_ts = int(time.time())

    @property
    def status(self) -> Dict[str, Any]:
        next_retry_at = 0
        if self._last_error_ts:
            next_retry_at = int(self._last_error_ts) + int(self._retry_after_sec)
        return {
            "enabled": self.enabled,
            "provider": "ollama" if self.enabled else "disabled",
            "last_error": self._last_error,
            "last_error_ts": self._last_error_ts,
            "next_retry_at": next_retry_at,
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
        if not self.client:
            return None
        if not self._can_attempt():
            return None
        try:
            return self.client.build_ingest_plan(
                session_key=session_key,
                user_text=user_text,
                assistant_text=assistant_text,
                ts=ts,
                metadata=metadata,
            )
        except BrainUnavailable as e:
            self._mark_error(str(e))
            return None

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
        if not self.client:
            return None
        if not self._can_attempt():
            return None
        try:
            return self.client.build_recall_plan(
                session_key=session_key,
                prompt=prompt,
                recent_messages=recent_messages,
                budgets=budgets,
                top_k=top_k,
                surface_threshold=surface_threshold,
                deep_enabled=deep_enabled,
            )
        except BrainUnavailable as e:
            self._mark_error(str(e))
            return None

    def build_merge_plan(
        self,
        *,
        session_key: str,
        memory_candidates: List[Dict[str, Any]],
        stats: Optional[Dict[str, Any]] = None,
    ) -> Optional[BrainMergePlan]:
        if not self.client:
            return None
        if not self._can_attempt():
            return None
        try:
            return self.client.build_merge_plan(
                session_key=session_key,
                memory_candidates=memory_candidates,
                stats=stats or {},
            )
        except BrainUnavailable as e:
            self._mark_error(str(e))
            return None

    def build_audit_patch_plan(
        self,
        *,
        text: str,
        allowed_languages: List[str],
        reasons: Optional[List[str]] = None,
    ) -> Optional[BrainAuditPatchPlan]:
        if not self.client:
            return None
        if not self._can_attempt():
            return None
        try:
            return self.client.build_audit_patch_plan(
                text=text,
                allowed_languages=allowed_languages,
                reasons=reasons or [],
            )
        except BrainUnavailable as e:
            self._mark_error(str(e))
            return None

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
