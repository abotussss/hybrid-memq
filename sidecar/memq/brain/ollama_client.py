from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .schemas import BrainAuditPatchPlan, BrainIngestPlan, BrainMergePlan, BrainRecallPlan

T = TypeVar("T", bound=BaseModel)


class BrainUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    timeout_ms: int
    keep_alive: str
    temperature: float
    max_tokens: int
    concurrent: int


def _read_prompt(name: str) -> str:
    p = Path(__file__).resolve().parent / "prompts" / name
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_json(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return "{}"
    if s.startswith("{") and s.endswith("}"):
        return s
    b = s.find("{")
    e = s.rfind("}")
    if b >= 0 and e > b:
        return s[b : e + 1]
    return "{}"


class OllamaBrainClient:
    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg
        self._sem = threading.Semaphore(max(1, int(cfg.concurrent)))
        self._cooldown_until = 0.0
        self._ingest_system = _read_prompt("ingest_system.txt")
        self._recall_system = _read_prompt("recall_system.txt")
        self._merge_system = _read_prompt("merge_system.txt")
        self._audit_patch_system = _read_prompt("audit_patch_system.txt")

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        if now < self._cooldown_until:
            raise BrainUnavailable("brain_cooldown")
        url = f"{self.cfg.base_url.rstrip('/')}{path}"
        timeout = max(0.5, float(self.cfg.timeout_ms) / 1000.0)
        with self._sem:
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=payload)
                    resp.raise_for_status()
                    obj = resp.json()
                    if not isinstance(obj, dict):
                        raise BrainUnavailable("brain_non_object_response")
                    return obj
            except BrainUnavailable:
                self._cooldown_until = time.time() + 8.0
                raise
            except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
                self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(f"brain_http_error:{type(e).__name__}") from e
            except Exception as e:
                self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(str(e)) from e

    def _chat_json(self, *, user_payload: Dict[str, Any], schema: Dict[str, Any], system_prompt: str) -> Dict[str, Any]:
        msg = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "format": schema,
            "options": {
                "temperature": float(self.cfg.temperature),
                "num_predict": int(self.cfg.max_tokens),
            },
            "messages": [
                {"role": "system", "content": system_prompt or "Return strict JSON only."},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        res = self._post("/api/chat", msg)
        content = ""
        message = res.get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or "")
        if not content:
            raise BrainUnavailable("brain_empty_content")
        try:
            parsed = json.loads(_extract_json(content))
            if not isinstance(parsed, dict):
                raise BrainUnavailable("brain_parsed_non_object")
            return parsed
        except Exception as e:
            raise BrainUnavailable(f"brain_invalid_json:{e}") from e

    def _call_schema(self, *, model_cls: Type[T], user_payload: Dict[str, Any], system_prompt: str) -> T:
        schema = model_cls.model_json_schema()
        data = self._chat_json(user_payload=user_payload, schema=schema, system_prompt=system_prompt)
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            raise BrainUnavailable(f"brain_schema_validation:{e}") from e

    def build_ingest_plan(self, *, session_key: str, user_text: str, assistant_text: str, ts: int, metadata: Optional[Dict[str, Any]]) -> BrainIngestPlan:
        payload = {
            "session_key": session_key,
            "ts": int(ts),
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
            "metadata": metadata or {},
            "constraints": {
                "version": "memq_brain_v1",
                "unknown_policy": "do_not_invent",
                "require_evidence_quote": True,
                "fact_key_prefix_allow": ["profile.", "pref.", "policy.", "project.", "relationship.", "timeline.", "rule.", "memory."],
            },
        }
        return self._call_schema(model_cls=BrainIngestPlan, user_payload=payload, system_prompt=self._ingest_system)

    def build_recall_plan(
        self,
        *,
        session_key: str,
        prompt: str,
        recent_messages: list[dict[str, Any]],
        budgets: dict[str, int],
        top_k: int,
        surface_threshold: float,
        deep_enabled: bool,
    ) -> BrainRecallPlan:
        payload = {
            "session_key": session_key,
            "prompt": prompt or "",
            "recent_messages": recent_messages[:8],
            "budgets": budgets,
            "retrieval_defaults": {
                "top_k": int(top_k),
                "surface_threshold": float(surface_threshold),
                "deep_enabled": bool(deep_enabled),
            },
            "constraints": {
                "version": "memq_brain_v1",
                "embedding": "disabled",
                "must_emit_nonempty_queries": True,
            },
        }
        return self._call_schema(model_cls=BrainRecallPlan, user_payload=payload, system_prompt=self._recall_system)

    def build_merge_plan(
        self,
        *,
        session_key: str,
        memory_candidates: list[dict[str, Any]],
        stats: Optional[dict[str, Any]] = None,
    ) -> BrainMergePlan:
        payload = {
            "session_key": session_key,
            "memory_candidates": memory_candidates[:120],
            "stats": stats or {},
            "constraints": {
                "version": "memq_brain_v1",
                "do_not_invent": True,
                "merge_only_from_input_ids": True,
            },
        }
        return self._call_schema(model_cls=BrainMergePlan, user_payload=payload, system_prompt=self._merge_system)

    def build_audit_patch_plan(
        self,
        *,
        text: str,
        allowed_languages: list[str],
        reasons: Optional[list[str]] = None,
    ) -> BrainAuditPatchPlan:
        payload = {
            "text": text,
            "allowed_languages": allowed_languages,
            "reasons": reasons or [],
            "constraints": {
                "version": "memq_brain_v1",
                "minimal_span_rewrite": True,
                "preserve_structure": True,
            },
        }
        return self._call_schema(
            model_cls=BrainAuditPatchPlan,
            user_payload=payload,
            system_prompt=self._audit_patch_system,
        )
