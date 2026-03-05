from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .schemas import BrainAuditPatchPlan, BrainIngestPlan, BrainMergePlan, BrainRecallPlan, StyleUpdatePlan

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
    required_mode: bool
    auto_restart: bool
    restart_cooldown_sec: int
    restart_wait_ms: int


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


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for k in ("content", "text", "thinking", "response", "message"):
            v = value.get(k)
            if isinstance(v, (str, dict, list)):
                t = _extract_text(v)
                if t:
                    parts.append(t)
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return ""
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for it in value:
            t2 = _extract_text(it)
            if t2:
                out.append(t2)
        return "\n".join(out)
    return ""


def _to_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _to_list(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if v is None:
        return []
    return [v]


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        x = int(v)
    except Exception:
        x = int(default)
    return max(lo, min(hi, x))


def _clamp_float(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        x = float(v)
    except Exception:
        x = float(default)
    return max(lo, min(hi, x))


def _clean_list(v: Any, *, max_len: int = 64, max_items: int = 16) -> list[str]:
    out: list[str] = []
    for item in _to_list(v):
        s = " ".join(str(item or "").split()).strip()
        if not s:
            continue
        if len(s) > max_len:
            s = s[:max_len]
        if s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _fallback_fts_queries(prompt: str, *, max_items: int = 4) -> list[str]:
    s = " ".join(str(prompt or "").split()).strip()
    if not s:
        return ["memory recall"]
    tokens: list[str] = []
    for m in re.findall(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff]{2,}", s):
        t = m.lower().strip()
        if len(t) < 2:
            continue
        if t not in tokens:
            tokens.append(t)
        if len(tokens) >= 10:
            break
    out: list[str] = []
    if tokens:
        out.append(" ".join(tokens[:4]))
        if len(tokens) > 4:
            out.append(" ".join(tokens[2:6]))
    out.append(s[:60])
    dedup: list[str] = []
    for q in out:
        q2 = " ".join(q.split()).strip()
        if not q2 or q2 in dedup:
            continue
        dedup.append(q2)
        if len(dedup) >= max_items:
            break
    return dedup or ["memory recall"]


def _normalize_budget_split(raw: Dict[str, Any], *, budget: int) -> Dict[str, int]:
    budget = max(16, min(400, int(budget)))
    split = {
        "profile": _clamp_int(raw.get("profile"), 0, budget, max(8, int(budget * 0.20))),
        "timeline": _clamp_int(raw.get("timeline"), 0, budget, max(8, int(budget * 0.25))),
        "surface": _clamp_int(raw.get("surface"), 0, budget, max(8, int(budget * 0.18))),
        "deep": _clamp_int(raw.get("deep"), 0, budget, max(8, int(budget * 0.30))),
        "ephemeral": _clamp_int(raw.get("ephemeral"), 0, budget, max(4, int(budget * 0.07))),
    }
    total = sum(split.values())
    if total <= budget:
        return split
    if total <= 0:
        return {
            "profile": max(8, int(budget * 0.20)),
            "timeline": max(8, int(budget * 0.25)),
            "surface": max(8, int(budget * 0.18)),
            "deep": max(8, int(budget * 0.30)),
            "ephemeral": max(4, int(budget * 0.07)),
        }
    # Scale down proportionally and then trim remainder from low-priority buckets.
    scaled = {k: max(0, int(v * budget / total)) for k, v in split.items()}
    remain = budget - sum(scaled.values())
    for k in ("deep", "timeline", "profile", "surface", "ephemeral"):
        if remain <= 0:
            break
        scaled[k] += 1
        remain -= 1
    while sum(scaled.values()) > budget:
        for k in ("ephemeral", "surface", "profile", "timeline", "deep"):
            if sum(scaled.values()) <= budget:
                break
            if scaled[k] > 0:
                scaled[k] -= 1
    return scaled


class OllamaBrainClient:
    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg
        self._sem = threading.Semaphore(max(1, int(cfg.concurrent)))
        self._cooldown_until = 0.0
        self._last_restart_attempt = 0.0
        self._restart_lock = threading.Lock()
        self._last_chat_stats: Dict[str, Any] = {}
        self._ingest_system = _read_prompt("ingest_system.txt")
        self._style_system = _read_prompt("style_system.txt")
        self._recall_system = _read_prompt("recall_system.txt")
        self._merge_system = _read_prompt("merge_system.txt")
        self._audit_patch_system = _read_prompt("audit_patch_system.txt")

    def _post_once(self, *, url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        t = httpx.Timeout(timeout=timeout, connect=min(3.0, timeout), read=timeout, write=timeout, pool=min(3.0, timeout))
        with httpx.Client(timeout=t) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            obj = resp.json()
            if not isinstance(obj, dict):
                raise BrainUnavailable("brain_non_object_response")
            self._last_chat_stats = {
                "prompt_eval_count": obj.get("prompt_eval_count"),
                "eval_count": obj.get("eval_count"),
                "total_duration": obj.get("total_duration"),
                "load_duration": obj.get("load_duration"),
                "eval_duration": obj.get("eval_duration"),
            }
            return obj

    def _probe_ollama_unlocked(self) -> bool:
        url = f"{self.cfg.base_url.rstrip('/')}/api/ps"
        timeout = min(5.0, max(0.5, float(self.cfg.timeout_ms) / 1000.0))
        try:
            t = httpx.Timeout(timeout=timeout, connect=min(2.0, timeout), read=timeout, write=timeout, pool=min(2.0, timeout))
            with httpx.Client(timeout=t) as client:
                resp = client.get(url)
                resp.raise_for_status()
                obj = resp.json()
                return isinstance(obj, dict)
        except Exception:
            return False

    def _warm_model(self) -> bool:
        url = f"{self.cfg.base_url.rstrip('/')}/api/chat"
        timeout = min(12.0, max(2.0, float(self.cfg.timeout_ms) / 1000.0))
        payload = self._build_chat_payload(
            user_payload={"ping": "memq"},
            system_prompt="Return {\"ok\":true} as JSON only.",
            schema=None,
            num_predict=64,
        )
        try:
            _ = self._post_once(url=url, payload=payload, timeout=timeout)
            return True
        except Exception:
            return False

    def _try_restart_ollama(self) -> bool:
        if not bool(self.cfg.auto_restart):
            return False
        now = time.time()
        if now - float(self._last_restart_attempt) < float(max(5, int(self.cfg.restart_cooldown_sec))):
            return False
        with self._restart_lock:
            now = time.time()
            if now - float(self._last_restart_attempt) < float(max(5, int(self.cfg.restart_cooldown_sec))):
                return False
            self._last_restart_attempt = now
            if self._probe_ollama_unlocked():
                return True
            try:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                return False
            time.sleep(max(0.25, float(self.cfg.restart_wait_ms) / 1000.0))
            return self._probe_ollama_unlocked()

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        if (not self.cfg.required_mode) and now < self._cooldown_until:
            raise BrainUnavailable("brain_cooldown")
        url = f"{self.cfg.base_url.rstrip('/')}{path}"
        timeout = max(0.5, float(self.cfg.timeout_ms) / 1000.0)
        with self._sem:
            try:
                return self._post_once(url=url, payload=payload, timeout=timeout)
            except BrainUnavailable:
                if not self.cfg.required_mode:
                    self._cooldown_until = time.time() + 8.0
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError, TimeoutError) as e:
                recovered = self._try_restart_ollama()
                if recovered:
                    try:
                        return self._post_once(url=url, payload=payload, timeout=timeout)
                    except Exception as e2:
                        if not self.cfg.required_mode:
                            self._cooldown_until = time.time() + 8.0
                        raise BrainUnavailable(f"brain_http_error:{type(e2).__name__}") from e2
                if not self.cfg.required_mode:
                    self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(f"brain_http_error:{type(e).__name__}") from e
            except httpx.HTTPStatusError as e:
                status = int(getattr(getattr(e, "response", None), "status_code", 0) or 0)
                transient = status in {408, 409, 425, 429, 500, 502, 503, 504}
                if transient:
                    time.sleep(max(0.2, min(1.2, float(self.cfg.restart_wait_ms) / 2500.0)))
                    try:
                        return self._post_once(url=url, payload=payload, timeout=timeout)
                    except Exception:
                        # Last chance: restart ollama and retry once.
                        recovered = self._try_restart_ollama()
                        if recovered:
                            try:
                                return self._post_once(url=url, payload=payload, timeout=timeout)
                            except Exception as e2:
                                if not self.cfg.required_mode:
                                    self._cooldown_until = time.time() + 8.0
                                raise BrainUnavailable(f"brain_http_error:{type(e2).__name__}") from e2
                if not self.cfg.required_mode:
                    self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(f"brain_http_error:{type(e).__name__}") from e
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                if not self.cfg.required_mode:
                    self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(f"brain_http_error:{type(e).__name__}") from e
            except Exception as e:
                if not self.cfg.required_mode:
                    self._cooldown_until = time.time() + 8.0
                raise BrainUnavailable(str(e)) from e

    @property
    def last_chat_stats(self) -> Dict[str, Any]:
        return dict(self._last_chat_stats or {})

    def get_ps_snapshot(self) -> Dict[str, Any]:
        url = f"{self.cfg.base_url.rstrip('/')}/api/ps"
        timeout = min(5.0, max(0.5, float(self.cfg.timeout_ms) / 1000.0))
        with self._sem:
            try:
                t = httpx.Timeout(timeout=timeout, connect=min(2.0, timeout), read=timeout, write=timeout, pool=min(2.0, timeout))
                with httpx.Client(timeout=t) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    obj = resp.json()
                    if not isinstance(obj, dict):
                        raise BrainUnavailable("brain_ps_non_object")
                    models = obj.get("models") if isinstance(obj.get("models"), list) else []
                    matched = None
                    for m in models:
                        if not isinstance(m, dict):
                            continue
                        name = str(m.get("name") or "")
                        model = str(m.get("model") or "")
                        if name == self.cfg.model or model == self.cfg.model or self.cfg.model in name or self.cfg.model in model:
                            matched = {
                                "name": name,
                                "model": model,
                                "size": m.get("size"),
                                "expires_at": m.get("expires_at"),
                            }
                            break
                    return {
                        "ok": True,
                        "seen": matched is not None,
                        "matched": matched,
                        "models_n": len(models),
                    }
            except (httpx.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
                raise BrainUnavailable(f"brain_ps_error:{type(e).__name__}") from e
            except Exception as e:
                raise BrainUnavailable(f"brain_ps_error:{type(e).__name__}") from e

    def ensure_runtime(self) -> Dict[str, Any]:
        ps = self.get_ps_snapshot()
        if bool(ps.get("seen")):
            return ps
        warmed = self._warm_model()
        if warmed:
            ps2 = self.get_ps_snapshot()
            if bool(ps2.get("seen")):
                return ps2
        restarted = self._try_restart_ollama()
        if restarted:
            self._warm_model()
            return self.get_ps_snapshot()
        return ps

    def _build_chat_payload(
        self,
        *,
        user_payload: Dict[str, Any],
        system_prompt: str,
        schema: Optional[Dict[str, Any]],
        num_predict: Optional[int] = None,
    ) -> Dict[str, Any]:
        np = int(num_predict if num_predict is not None else self.cfg.max_tokens)
        np = max(64, min(2048, np))
        msg: Dict[str, Any] = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "think": False,
            "options": {
                "temperature": float(self.cfg.temperature),
                "num_predict": np,
            },
            "messages": [
                {"role": "system", "content": system_prompt or "Return strict JSON only."},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        if schema:
            msg["format"] = schema
        return msg

    def _parse_chat_json(self, res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidates: list[str] = []
        message = res.get("message")
        if isinstance(message, dict):
            for key in ("content", "text", "thinking"):
                c = _extract_text(message.get(key)).strip()
                if c:
                    candidates.append(c)
        for key in ("response", "content", "text", "thinking"):
            c2 = _extract_text(res.get(key)).strip()
            if c2:
                candidates.append(c2)
        full = _extract_text(res).strip()
        if full:
            candidates.append(full)

        seen: set[str] = set()
        for c3 in candidates:
            if not c3 or c3 in seen:
                continue
            seen.add(c3)
            try:
                parsed = json.loads(_extract_json(c3))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    def _chat_json(
        self,
        *,
        user_payload: Dict[str, Any],
        schema: Dict[str, Any],
        system_prompt: str,
        num_predict: Optional[int] = None,
    ) -> Dict[str, Any]:
        msg = self._build_chat_payload(user_payload=user_payload, system_prompt=system_prompt, schema=schema, num_predict=num_predict)
        res = self._post("/api/chat", msg)
        parsed = self._parse_chat_json(res)
        if parsed is not None and len(parsed) > 0:
            return parsed

        # Retry once without strict format; some local model builds occasionally
        # return empty content under schema-constrained generation.
        relaxed_system = (
            "Return one minified JSON object only. "
            "No prose. No markdown. No explanations."
        )
        relaxed_np = int(num_predict if num_predict is not None else self.cfg.max_tokens)
        relaxed_np = max(64, min(relaxed_np, 160))
        msg2 = self._build_chat_payload(
            user_payload=user_payload,
            system_prompt=relaxed_system,
            schema=None,
            num_predict=relaxed_np,
        )
        res2 = self._post("/api/chat", msg2)
        parsed2 = self._parse_chat_json(res2)
        if parsed2 is not None:
            return parsed2
        raise BrainUnavailable("brain_empty_content")

    def _repair_payload(self, *, model_cls: Type[T], data: Dict[str, Any], user_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw = _to_dict(data)
        if model_cls is StyleUpdatePlan:
            su_raw = _to_dict(raw.get("style_update")) or raw
            keys = _to_dict(su_raw.get("keys"))
            if not keys:
                keys = _to_dict(raw.get("style")) or _to_dict(raw.get("style_profile"))
            apply_raw = su_raw.get("apply")
            explicit_raw = su_raw.get("explicit")
            apply = bool(apply_raw) if isinstance(apply_raw, bool) else bool(keys)
            explicit = bool(explicit_raw) if isinstance(explicit_raw, bool) else bool(keys)
            cleaned_keys: Dict[str, str] = {}
            for k, v in keys.items():
                kk = " ".join(str(k or "").split()).strip()
                vv = " ".join(str(v or "").split()).strip()
                if not kk or not vv:
                    continue
                cleaned_keys[kk[:48]] = vv[:180]
                if len(cleaned_keys) >= 12:
                    break
            return {"apply": apply, "explicit": explicit, "keys": cleaned_keys}
        if model_cls is BrainRecallPlan:
            retrieval_defaults = _to_dict(user_payload.get("retrieval_defaults"))
            budgets = _to_dict(user_payload.get("budgets"))
            memctx_budget = _clamp_int(budgets.get("memctxTokens"), 16, 400, 120)
            intent_raw = _to_dict(raw.get("intent"))
            repaired: Dict[str, Any] = {
                "version": "memq_brain_v1",
                "intent": {
                    "timeline": _clamp_float(intent_raw.get("timeline"), 0.0, 1.0, 0.0),
                    "profile": _clamp_float(intent_raw.get("profile"), 0.0, 1.0, 0.0),
                    "state": _clamp_float(intent_raw.get("state"), 0.0, 1.0, 0.0),
                    "fact_lookup": _clamp_float(intent_raw.get("fact_lookup"), 0.0, 1.0, 0.0),
                    "meta": _clamp_float(intent_raw.get("meta"), 0.0, 1.0, 0.0),
                    "fact": _clamp_float(intent_raw.get("fact"), 0.0, 1.0, 0.0),
                    "procedure": _clamp_float(intent_raw.get("procedure"), 0.0, 1.0, 0.0),
                    "overview": _clamp_float(intent_raw.get("overview"), 0.0, 1.0, 0.0),
                },
                "entity_hints": _clean_list(raw.get("entity_hints"), max_len=64, max_items=16),
                "fact_keys": [x.lower() for x in _clean_list(raw.get("fact_keys"), max_len=96, max_items=32)],
                "fts_queries": _clean_list(raw.get("fts_queries"), max_len=120, max_items=8),
                "budget_split": _normalize_budget_split(_to_dict(raw.get("budget_split")), budget=memctx_budget),
                "retrieval": {
                    "topk_surface": _clamp_int(
                        _to_dict(raw.get("retrieval")).get("topk_surface"),
                        1,
                        50,
                        _clamp_int(retrieval_defaults.get("top_k"), 1, 50, 4),
                    ),
                    "topk_deep": _clamp_int(
                        _to_dict(raw.get("retrieval")).get("topk_deep"),
                        1,
                        50,
                        _clamp_int(retrieval_defaults.get("top_k"), 1, 50, 5),
                    ),
                    "topk_events": _clamp_int(
                        _to_dict(raw.get("retrieval")).get("topk_events"),
                        1,
                        50,
                        _clamp_int(retrieval_defaults.get("top_k"), 1, 50, 4),
                    ),
                    "allow_deep": bool(
                        _to_dict(raw.get("retrieval")).get(
                            "allow_deep",
                            bool(retrieval_defaults.get("deep_enabled", True)),
                        )
                    ),
                },
            }
            if repaired["intent"]["fact_lookup"] <= 0.0 and repaired["intent"]["fact"] > 0.0:
                repaired["intent"]["fact_lookup"] = repaired["intent"]["fact"]
            if not repaired["fts_queries"]:
                repaired["fts_queries"] = _fallback_fts_queries(str(user_payload.get("prompt") or ""), max_items=4)
            tr = _to_dict(raw.get("time_range"))
            start = str(tr.get("startDay") or tr.get("start_day") or tr.get("start") or "").strip()
            end = str(tr.get("endDay") or tr.get("end_day") or tr.get("end") or "").strip()
            label = str(tr.get("label") or "recent").strip() or "recent"
            if start and end:
                repaired["time_range"] = {"startDay": start, "endDay": end, "label": label[:24]}
            return repaired
        if model_cls is BrainIngestPlan:
            facts_raw = _to_list(raw.get("facts"))
            events_raw = _to_list(raw.get("events"))
            quarantine = _to_list(raw.get("quarantine"))
            fixed_facts: list[dict[str, Any]] = []
            for f in facts_raw:
                if not isinstance(f, dict):
                    continue
                fk = str(f.get("fact_key") or f.get("key") or f.get("k") or "").strip().lower()
                val = f.get("value")
                if val is None:
                    val = f.get("v")
                val_s = " ".join(str(val or "").split()).strip()
                if not fk or not val_s:
                    continue
                layer = str(f.get("layer") or "").strip().lower()
                conf_raw = f.get("confidence")
                conf = 0.62
                if isinstance(conf_raw, (int, float)):
                    conf = float(conf_raw)
                elif isinstance(conf_raw, str):
                    tag = conf_raw.strip().lower()
                    if tag in {"deep", "surface", "ephemeral"} and not layer:
                        layer = tag
                    elif tag:
                        try:
                            conf = float(tag)
                        except Exception:
                            pass
                if layer not in {"deep", "surface", "ephemeral"}:
                    layer = "deep" if fk.startswith(("profile.", "policy.", "pref.", "rule.")) else "surface"
                if layer == "deep" and conf < 0.7:
                    conf = 0.7
                if layer == "surface" and conf < 0.55:
                    conf = 0.55
                if layer == "ephemeral" and conf > 0.65:
                    conf = 0.65
                ttl_days = _clamp_int(
                    f.get("ttl_days") or f.get("ttl") or f.get("ttlDays"),
                    1,
                    3650,
                    365 if layer == "deep" else 21,
                )
                fixed_facts.append(
                    {
                        "entity_id": str(f.get("entity_id") or f.get("entity") or "ent:user")[:64],
                        "fact_key": fk[:96],
                        "value": val_s[:160],
                        "confidence": _clamp_float(conf, 0.0, 1.0, 0.62),
                        "layer": layer,
                        "ttl_days": ttl_days,
                        "keywords": _clean_list(f.get("keywords"), max_len=40, max_items=16),
                        "evidence_quote": " ".join(str(f.get("evidence_quote") or f.get("evidence") or "").split())[:120],
                    }
                )
            if not fixed_facts:
                # Some local model runs emit a compact fallback like:
                # {"facts":"..."}  -> preserve as durable generic memory note.
                facts_str = " ".join(_clean_list(raw.get("facts"), max_len=160, max_items=4)).strip()
                if facts_str:
                    fixed_facts.append(
                        {
                            "entity_id": "ent:user",
                            "fact_key": "memory.note.generic",
                            "value": facts_str[:160],
                            "confidence": 0.6,
                            "layer": "deep",
                            "ttl_days": 180,
                            "keywords": _clean_list(raw.get("keywords"), max_len=40, max_items=8),
                            "evidence_quote": "",
                        }
                    )

            fixed_events: list[dict[str, Any]] = []
            for ev in events_raw:
                if not isinstance(ev, dict):
                    continue
                summary = " ".join(str(ev.get("summary") or ev.get("text") or "").split()).strip()
                if not summary:
                    continue
                kind = str(ev.get("kind") or "chat").strip().lower()
                if kind not in {"chat", "action", "decision", "progress", "error", "plan"}:
                    kind = "chat"
                actor = str(ev.get("actor") or "assistant").strip().lower()
                if actor not in {"user", "assistant", "tool"}:
                    actor = "assistant"
                fixed_events.append(
                    {
                        "day": str(ev.get("day") or ev.get("day_key") or "")[:10],
                        "ts": _clamp_int(ev.get("ts"), 0, 2_147_483_647, int(time.time())),
                        "summary": summary[:320],
                        "salience": _clamp_float(ev.get("salience"), 0.0, 1.0, 0.5),
                        "ttl_days": _clamp_int(ev.get("ttl_days") or ev.get("ttl"), 1, 3650, 30),
                        "keywords": _clean_list(ev.get("keywords"), max_len=40, max_items=16),
                        "kind": kind,
                        "actor": actor,
                    }
                )

            repaired = {
                "version": "memq_brain_v1",
                "facts": fixed_facts,
                "events": fixed_events,
                "style_update": _to_dict(raw.get("style_update")) or None,
                "rules_update": _to_dict(raw.get("rules_update")) or None,
                "quarantine": [x for x in quarantine if isinstance(x, dict)],
            }
            return repaired
        if model_cls is BrainMergePlan:
            return {
                "version": "memq_brain_v1",
                "merges": [x for x in _to_list(raw.get("merges")) if isinstance(x, dict)],
                "prunes": [x for x in _to_list(raw.get("prunes")) if isinstance(x, dict)],
            }
        if model_cls is BrainAuditPatchPlan:
            return {
                "version": "memq_brain_v1",
                "patched_text": str(raw.get("patched_text") or user_payload.get("text") or ""),
                "changed_spans": [x for x in _to_list(raw.get("changed_spans")) if isinstance(x, dict)],
            }
        return None

    def _call_schema(
        self,
        *,
        model_cls: Type[T],
        user_payload: Dict[str, Any],
        system_prompt: str,
        num_predict: Optional[int] = None,
    ) -> T:
        schema = model_cls.model_json_schema()
        data = self._chat_json(user_payload=user_payload, schema=schema, system_prompt=system_prompt, num_predict=num_predict)
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            repaired = self._repair_payload(model_cls=model_cls, data=data, user_payload=user_payload)
            if repaired is not None:
                try:
                    return model_cls.model_validate(repaired)
                except ValidationError as e2:
                    raise BrainUnavailable(f"brain_schema_validation:{e2}") from e2
            raise BrainUnavailable(f"brain_schema_validation:{e}") from e

    def build_ingest_plan(self, *, session_key: str, user_text: str, assistant_text: str, ts: int, metadata: Optional[Dict[str, Any]]) -> BrainIngestPlan:
        ingest_np = max(96, min(int(self.cfg.max_tokens), 192))
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
        return self._call_schema(
            model_cls=BrainIngestPlan,
            user_payload=payload,
            system_prompt=self._ingest_system,
            num_predict=ingest_np,
        )

    def build_style_update_plan(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        ts: int,
        metadata: Optional[Dict[str, Any]],
    ) -> StyleUpdatePlan:
        payload = {
            "session_key": session_key,
            "ts": int(ts),
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
            "metadata": metadata or {},
            "constraints": {
                "version": "memq_brain_v1",
                "style_keys_allow": ["tone", "persona", "verbosity", "firstPerson", "callUser", "prefix", "speakingStyle", "avoid"],
                "unknown_policy": "return_apply_false",
            },
        }
        user_block = (
            f"user_text:\n{user_text or ''}\n\n"
            f"assistant_text:\n{assistant_text or ''}\n"
        )
        msg = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": float(self.cfg.temperature),
            },
            "messages": [
                {"role": "system", "content": self._style_system or self._ingest_system or "Return strict JSON only."},
                {"role": "user", "content": user_block},
            ],
        }
        res = self._post("/api/chat", msg)
        parsed = self._parse_chat_json(res)
        if parsed is None or len(parsed) == 0:
            relaxed = dict(msg)
            relaxed["messages"] = [
                {
                    "role": "system",
                    "content": "Return one minified JSON object only with keys apply, explicit, keys. No prose.",
                },
                {"role": "user", "content": user_block},
            ]
            res2 = self._post("/api/chat", relaxed)
            parsed = self._parse_chat_json(res2) or {}
        try:
            plan = StyleUpdatePlan.model_validate(parsed)
        except ValidationError:
            repaired = self._repair_payload(model_cls=StyleUpdatePlan, data=parsed or {}, user_payload=payload)
            if repaired is None:
                plan = StyleUpdatePlan(apply=False, explicit=False, keys={})
            else:
                plan = StyleUpdatePlan.model_validate(repaired)

        return plan

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
        recall_np = max(96, min(int(self.cfg.max_tokens), 160))
        compact_recent: list[dict[str, Any]] = []
        for m in (recent_messages or [])[:6]:
            role = str((m or {}).get("role") or "")
            text = " ".join(str((m or {}).get("text") or "").split()).strip()[:220]
            ts = (m or {}).get("ts")
            compact_recent.append({"role": role, "text": text, "ts": ts})
        payload = {
            "session_key": session_key,
            "prompt": prompt or "",
            "recent_messages": compact_recent,
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
        return self._call_schema(
            model_cls=BrainRecallPlan,
            user_payload=payload,
            system_prompt=self._recall_system,
            num_predict=recall_np,
        )

    def build_merge_plan(
        self,
        *,
        session_key: str,
        memory_candidates: list[dict[str, Any]],
        stats: Optional[dict[str, Any]] = None,
    ) -> BrainMergePlan:
        merge_np = max(96, min(int(self.cfg.max_tokens), 144))
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
        return self._call_schema(
            model_cls=BrainMergePlan,
            user_payload=payload,
            system_prompt=self._merge_system,
            num_predict=merge_np,
        )

    def build_audit_patch_plan(
        self,
        *,
        text: str,
        allowed_languages: list[str],
        reasons: Optional[list[str]] = None,
    ) -> BrainAuditPatchPlan:
        audit_np = max(64, min(int(self.cfg.max_tokens), 128))
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
            num_predict=audit_np,
        )
