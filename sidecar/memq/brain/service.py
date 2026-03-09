from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any, TYPE_CHECKING

from sidecar.memq.config import Config
from sidecar.memq.db import MemqDB
from sidecar.memq.memory_source import list_qrule, list_qstyle
from sidecar.memq.brain.schemas import (
    BrainAuditPatchPlan,
    BrainIngestPlan,
    BrainMergePlan,
    BrainPreviewPlan,
    BrainRecallPlan,
)
from sidecar.memq.brain.ollama_client import BrainUnavailable, OllamaClient, load_prompt

if TYPE_CHECKING:
    from sidecar.memq.lancedb_bridge import LanceDbMemoryBackend


FACT_KEY_PREFIXES = ("profile.", "pref.", "policy.", "project.", "relationship.", "timeline.")
STYLE_KEYS = {"tone", "persona", "verbosity", "speaking_style", "callUser", "firstPerson", "prefix"}
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")
GLOBAL_SESSION_KEY = "global"
STRICT_TRUE_RULE_KEYS = {
    "security.never_output_secrets",
    "security.no_api_keys",
    "security.no_api_tokens",
    "security.no_tokens",
    "security.no_secrets",
    "output.redact_secret_like",
}


STYLE_NOISE_TERMS = (
    "memory-lancedb-pro",
    "lancedb",
    "qctx",
    "qstyle",
    "qrule",
    "memctx",
    "memstyle",
    "memrule",
    "memq",
    "openclaw",
    "sqlite",
    "ollama",
    "backend",
    "adapter",
    "bridge",
    "helper",
)

RUNTIME_BLOCK_RE = re.compile(
    r"<(?:QSTYLE|QRULE|QCTX)\b[^>]*>[\s\S]*?</(?:QSTYLE|QRULE|QCTX)\b[^>]*>",
    re.IGNORECASE,
)
def _compact_text(text: str, *, limit: int) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)].rstrip() + "…"


def _strip_runtime_blocks(text: str) -> str:
    raw = str(text or "")
    stripped = RUNTIME_BLOCK_RE.sub(" ", raw)
    return " ".join(stripped.split())


def _turn_event_text(text: str) -> str:
    return " ".join(_strip_runtime_blocks(text).split()).strip()


def _append_turn_event(
    *,
    lancedb_primary: bool,
    db: MemqDB,
    session_key: str,
    lancedb_entries: list[dict[str, Any]],
    event_payloads: list[dict[str, Any]],
    wrote: dict[str, int],
    ts: int,
    actor: str,
    text: str,
    kind: str = "chat",
    salience: float = 0.45,
) -> None:
    raw_text = _turn_event_text(text)[:400]
    if len(raw_text) < 8:
        return
    summary = raw_text[:160]
    duplicate = next(
        (
            True
            for item in event_payloads
            if str(item.get("actor") or "") == actor
            and str(item.get("text") or "") == raw_text
        ),
        False,
    )
    if duplicate:
        return
    if not lancedb_primary:
        db.insert_event(
            session_key=session_key,
            ts=ts,
            actor=actor,
            kind=kind,
            summary=summary,
            salience=salience,
            keywords=[],
            ttl_days=21,
        )
    event_payloads.append(
        {
            "summary": summary,
            "kind": kind,
            "actor": actor,
            "ts": ts,
            "salience": salience,
            "text": raw_text,
        }
    )
    lancedb_entries.append(
        {
            "id": f"{session_key}:event:{kind}:{actor}:{ts}:raw:{wrote['events']}",
            "session_key": session_key,
            "layer": "surface",
            "kind": "event",
            "fact_key": f"event.{kind}.{actor}",
            "value": summary,
            "text": raw_text,
            "summary": summary,
            "importance": salience,
            "confidence": 1.0,
            "strength": salience,
            "timestamp": ts,
        }
    )
    wrote["events"] += 1


def _extract_quoted_name(text: str) -> str:
    for pattern in (r"「([^」]{2,80})」", r'"([^"]{2,80})"'):
        match = re.search(pattern, text)
        if match:
            return " ".join(match.group(1).split())
    return ""


def _sanitize_call_user(value: str) -> str:
    clean = " ".join(str(value or "").split()).strip("「」\"' ")
    if not clean:
        return ""
    if match := re.search(r"(?:俺|ぼく|僕|私|わたし)の(?:こと|名前)は\s*([^\s、。！!？?\n]{1,24})", clean):
        clean = match.group(1)
    elif match := re.search(r"([^\s、。！!？?\n]{1,24})\s*って呼んで", clean):
        clean = match.group(1)
    clean = re.split(r"\s+\d+\.\s*", clean, maxsplit=1)[0]
    clean = re.split(r"\s+(?:基本トーン|口調|トーン|性格|特徴的な|思考回路|行動原理|役割|関係性)\b", clean, maxsplit=1)[0]
    clean = re.split(r"[、。!！?？\n]", clean, maxsplit=1)[0]
    clean = clean.strip("「」\"' ")
    if len(clean) > 24:
        return ""
    return clean


def _strip_following_sections(value: str) -> str:
    clean = " ".join(str(value or "").split()).strip()
    if not clean:
        return ""
    clean = re.split(r"\s+\d+\.\s*", clean, maxsplit=1)[0]
    clean = re.split(
        r"\s*(?:二人称|ユーザーに対する呼称|基本トーン|口調|トーン|ユーザーへの接し方|感情表現|特徴的な語尾・言い回し|思考回路|行動原理|役割|関係性)\s*[:：]?",
        clean,
        maxsplit=1,
    )[0]
    return clean.strip("「」\"' ")


def _sanitize_first_person(value: str) -> str:
    clean = _strip_following_sections(value)
    clean = re.split(r"[、。!！?？\s]", clean, maxsplit=1)[0]
    clean = clean.strip("「」\"' ")
    if len(clean) > 16:
        return ""
    return clean


def _sanitize_style_sentence(value: str) -> str:
    clean = _strip_following_sections(value)
    clean = clean.strip("「」\"' ")
    if len(clean) > 180:
        clean = clean[:180].rstrip()
    return clean


def _style_value_is_noise(key: str, value: str) -> bool:
    clean = " ".join(str(value or "").split()).strip()
    if not clean:
        return True
    lowered = clean.lower()
    placeholder_exact = {"〜", "~", "-", "--", "...", "…", "n/a", "na", "none", "null", "unknown"}
    if lowered in placeholder_exact:
        return True
    if re.fullmatch(r"[〜~.…\-\s]+", clean):
        return True
    if key == "persona":
        if any(term in lowered for term in STYLE_NOISE_TERMS):
            return True
        if lowered in {"persona", "character", "role", "assistant", "generic"}:
            return True
    if key in {"tone", "speaking_style"}:
        if lowered in {"neutral", "none", "default", "generic"}:
            return True
    return False


def _extract_explicit_style_hints(text: str) -> dict[str, str]:
    raw = str(text or "")
    hints: dict[str, str] = {}
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    if match := re.search(r"(?:一人称|first ?person)\s*(?:は|:|：)\s*([^\s、。]+)", raw, re.IGNORECASE):
        candidate = _sanitize_first_person(match.group(1))
        if candidate:
            hints["firstPerson"] = candidate
    if match := re.search(r"(?:呼び方|呼称)(?:は|:|：)\s*[「\"]?([^」\"\n。]{1,24})", raw):
        candidate = _sanitize_call_user(match.group(1))
        if candidate:
            hints["callUser"] = candidate
    elif match := re.search(r"([^\s、。]{1,24})って呼んで", raw):
        candidate = _sanitize_call_user(match.group(1))
        if candidate:
            hints["callUser"] = candidate
    elif match := re.search(r"(?:俺|ぼく|僕|私|わたし)の名前は\s*([^\s、。！!？?\n]{1,24})", raw):
        candidate = _sanitize_call_user(match.group(1))
        if candidate:
            hints["callUser"] = candidate

    if match := re.search(r"ペルソナ(?:は|:|：)\s*([^\n。]{2,120})", raw):
        hints["persona"] = match.group(1).strip("「」\"' ")
    else:
        for line in lines:
            if "として振る舞" in line or "として話して" in line or "として会話" in line:
                if "あなたは" in line:
                    persona = line.split("あなたは", 1)[1]
                    persona = re.split(r"として振る舞|として話して|として会話", persona, maxsplit=1)[0]
                    persona = persona.strip("。 ")
                    if persona:
                        quoted = _extract_quoted_name(persona)
                        hints["persona"] = (quoted or persona)[:240]
                        break
        if "persona" not in hints:
            quoted = _extract_quoted_name(raw)
            if quoted:
                hints["persona"] = quoted

    for line in lines:
        if line.startswith("基本トーン:") or line.startswith("基本トーン："):
            raw_tone = line.split(":", 1)[1].strip() if ":" in line else line.split("：", 1)[1].strip()
            candidate = _sanitize_style_sentence(raw_tone)
            if candidate:
                hints["tone"] = candidate
        elif line.startswith("特徴的な語尾・言い回し") and ("：" in line or ":" in line):
            raw_style = line.split("：", 1)[1].strip() if "：" in line else line.split(":", 1)[1].strip()
            candidate = _sanitize_style_sentence(raw_style)
            if candidate:
                hints["speaking_style"] = candidate

    if "tone" not in hints:
        if match := re.search(r"基本トーン\s*(?:は|:|：)\s*(.+)", raw):
            candidate = _sanitize_style_sentence(match.group(1))
            if candidate:
                hints["tone"] = candidate
    if "speaking_style" not in hints:
        if match := re.search(r"特徴的な語尾・言い回し\s*(?:は|:|：)\s*(.+)", raw):
            candidate = _sanitize_style_sentence(match.group(1))
            if candidate:
                hints["speaking_style"] = candidate

    for key, value in list(hints.items()):
        clean = " ".join(str(value or "").split()).strip("「」\"' ")
        clean = re.sub(r"(?:だよ|だね|です|だ|です。|だよ。|だね。)$", "", clean).strip()
        if not clean or _style_value_is_noise(key, clean):
            hints.pop(key, None)
        else:
            hints[key] = clean[:240]
    return hints


def _clean_style_value(key: str, value: str, *, user_text: str = "") -> str:
    hints = _extract_explicit_style_hints(user_text)
    raw = " ".join(str(value or "").split()).strip("「」\"' ")
    hint_value = " ".join(str(hints.get(key) or "").split()).strip("「」\"' ")

    if key == "callUser":
        candidate = _sanitize_call_user(hint_value or raw)
    elif key == "firstPerson":
        candidate = _sanitize_first_person(hint_value or raw)
    elif key in {"tone", "speaking_style"}:
        candidate = _sanitize_style_sentence(hint_value or raw)
    elif key == "persona":
        candidate = _strip_following_sections(hint_value or raw)
    else:
        candidate = hint_value or raw

    candidate = " ".join(str(candidate or "").split()).strip("「」\"' ")
    if not candidate:
        return ""
    if _style_value_is_noise(key, candidate):
        fallback = " ".join(str(hint_value or "").split()).strip("「」\"' ")
        if fallback and not _style_value_is_noise(key, fallback):
            return fallback[:240]
        return ""
    return candidate[:240]


def _should_apply_style_patch_key(
    *,
    key: str,
    actual_value: str,
    current_value: str,
    explicit_hints: dict[str, str],
) -> bool:
    if not actual_value:
        return False
    if not current_value:
        return True
    if actual_value == current_value:
        return False
    if not explicit_hints:
        return True
    if key in explicit_hints:
        return True
    return False


def _compact_messages(messages: list[dict[str, Any]], *, max_messages: int = 4, max_chars: int = 220) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in messages[-max_messages:]:
        compact.append(
            {
                "role": str(item.get("role") or "user"),
                "text": _compact_text(str(item.get("text") or ""), limit=max_chars),
                "ts": item.get("ts"),
            }
        )
    return compact


def _lancedb_digest_entries(
    *,
    session_key: str,
    ts: int,
    events: list[dict[str, Any]],
    fallback_summary: str,
) -> list[dict[str, Any]]:
    if not events and not fallback_summary:
        return []
    day_key = datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d")
    summaries = [str(item.get("summary") or "").strip() for item in events if str(item.get("summary") or "").strip()]
    if not summaries and fallback_summary:
        summaries = [fallback_summary]
    digest_micro = " | ".join(summaries[:3])[:220]
    digest_meso = " | ".join(summaries[:6])[:480]
    if not digest_micro:
        return []
    return [
        {
            "id": f"{session_key}:digest:{day_key}:{ts}",
            "session_key": session_key,
            "layer": "surface",
            "kind": "digest",
            "fact_key": f"digest.{day_key}",
            "value": digest_micro,
            "text": digest_meso or digest_micro,
            "summary": digest_micro,
            "importance": 0.8,
            "confidence": 1.0,
            "strength": 0.8,
            "timestamp": ts,
        }
    ]


def _compact_mapping(values: dict[str, str], *, max_items: int = 8, max_value_chars: int = 120) -> dict[str, str]:
    compact: dict[str, str] = {}
    for key in sorted(values.keys())[:max_items]:
        compact[str(key)] = _compact_text(str(values.get(key) or ""), limit=max_value_chars)
    return compact


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


def _normalize_rule_value(key: str, value: str) -> str:
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if key == "language.allowed":
        parts = [part.strip() for part in raw.replace("、", ",").replace(" ", ",").split(",") if part.strip()]
        seen: list[str] = []
        for part in parts:
            token = part.lower()
            if token not in seen:
                seen.append(token)
        return ",".join(seen)
    if key in STRICT_TRUE_RULE_KEYS:
        if lowered in {"true", "1", "yes", "on", "enable", "enabled"}:
            return "true"
        return ""
    if lowered in {"false", "0", "no", "off", "disable", "disabled"} and (key.startswith("security.") or key.startswith("output.")):
        return ""
    return raw


def _should_apply_rule_patch_key(
    *,
    key: str,
    actual_value: str,
    current_value: str,
) -> bool:
    if not actual_value:
        return False
    current = " ".join(str(current_value or "").split()).strip()
    if not current:
        return True
    if actual_value == current:
        return False
    if key in STRICT_TRUE_RULE_KEYS:
        return current != "true" and actual_value == "true"
    return False


def _explicit_targets(session_key: str) -> tuple[str, ...]:
    raw = str(session_key or "").strip() or GLOBAL_SESSION_KEY
    if raw == GLOBAL_SESSION_KEY:
        return (GLOBAL_SESSION_KEY,)
    return (raw, GLOBAL_SESSION_KEY)


def _current_qstyle(
    db: MemqDB,
    memory_backend: "LanceDbMemoryBackend | None",
    session_key: str,
) -> dict[str, str]:
    if memory_backend is not None and memory_backend.enabled() and hasattr(memory_backend, "list_entries"):
        return list_qstyle(db, memory_backend, session_key)
    return db.list_style(session_key)


def _current_qrule(
    db: MemqDB,
    memory_backend: "LanceDbMemoryBackend | None",
    session_key: str,
) -> dict[str, str]:
    if memory_backend is not None and memory_backend.enabled() and hasattr(memory_backend, "list_entries"):
        return list_qrule(db, memory_backend, session_key)
    return db.list_rules(session_key)


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

    async def _call(
        self,
        *,
        session_key: str,
        op: str,
        system_prompt: str,
        user_prompt: str,
        schema_model: type[Any],
        max_tokens: int | None = None,
    ) -> tuple[Any, str, dict[str, Any]]:
        trace_id = str(uuid.uuid4())
        import time

        t0 = time.perf_counter()
        async with self._lock:
            try:
                res = await self.client.chat_schema(
                    system=system_prompt,
                    user=user_prompt,
                    schema_model=schema_model,
                    max_tokens=max_tokens,
                )
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
                "user_text": _compact_text(user_text, limit=960),
                # Assistant-side runtime dumps must not become new style/rule truth.
                "assistant_text": _compact_text(_strip_runtime_blocks(assistant_text), limit=180),
                "current_style": _compact_mapping(current_style, max_items=4, max_value_chars=72),
                "current_rules": _compact_mapping(current_rules, max_items=4, max_value_chars=72),
                "recent_summary": _compact_text(recent_summary, limit=180),
            },
            ensure_ascii=False,
        )
        return await self._call(
            session_key=session_key,
            op="ingest_plan",
            system_prompt=system,
            user_prompt=user,
            schema_model=BrainIngestPlan,
            max_tokens=self.cfg.brain.ingest_max_tokens,
        )

    async def build_preview_ingest_plan(
        self,
        *,
        session_key: str,
        user_text: str,
        current_style: dict[str, str],
        current_rules: dict[str, str],
    ) -> tuple[BrainPreviewPlan, str, dict[str, Any]]:
        system = self._prompt(
            "preview_system.txt",
            "Return JSON only. Extract only explicit style_update and rules_update for the current turn.",
        )
        user = json.dumps(
            {
                "session_key": session_key,
                "user_text": _compact_text(user_text, limit=960),
                "current_style": _compact_mapping(current_style, max_items=4, max_value_chars=72),
                "current_rules": _compact_mapping(current_rules, max_items=4, max_value_chars=72),
                "preview_only": True,
            },
            ensure_ascii=False,
        )
        max_tokens = min(self.cfg.brain.ingest_max_tokens, 224)
        try:
            plan, trace_id, meta = await self._call(
                session_key=session_key,
                op="preview_ingest_plan",
                system_prompt=system,
                user_prompt=user,
                schema_model=BrainPreviewPlan,
                max_tokens=max_tokens,
            )
            if not (
                (plan.style_update and plan.style_update.apply)
                or (plan.rules_update and plan.rules_update.apply)
            ):
                recovery_user = json.dumps(
                    {
                        "session_key": session_key,
                        "user_text": _compact_text(user_text, limit=420),
                        "current_style": _compact_mapping(current_style, max_items=3, max_value_chars=56),
                        "current_rules": _compact_mapping(current_rules, max_items=3, max_value_chars=56),
                        "preview_only": True,
                        "recovery_mode": True,
                        "instruction": "Re-evaluate whether this turn contains an explicit QSTYLE or QRULE update. If it changes how the user should be addressed, update only callUser. If it adds or tightens safety/privacy constraints, emit only the missing canonical rule keys. If no explicit update is requested, leave both apply=false.",
                    },
                    ensure_ascii=False,
                )
                return await self._call(
                    session_key=session_key,
                    op="preview_ingest_plan_recovery",
                    system_prompt=system,
                    user_prompt=recovery_user,
                    schema_model=BrainPreviewPlan,
                    max_tokens=min(max_tokens, 160),
                )
            return plan, trace_id, meta
        except BrainUnavailable:
            recovery_user = json.dumps(
                {
                    "session_key": session_key,
                    "user_text": _compact_text(user_text, limit=420),
                    "current_style": _compact_mapping(current_style, max_items=3, max_value_chars=56),
                    "current_rules": _compact_mapping(current_rules, max_items=3, max_value_chars=56),
                    "preview_only": True,
                    "recovery_mode": True,
                },
                ensure_ascii=False,
            )
            return await self._call(
                session_key=session_key,
                op="preview_ingest_plan_recovery",
                system_prompt=system,
                user_prompt=recovery_user,
                schema_model=BrainPreviewPlan,
                max_tokens=min(max_tokens, 160),
            )

    def apply_preview_plan(
        self,
        db: MemqDB,
        *,
        session_key: str,
        plan: BrainPreviewPlan,
        ts: int,
        user_text: str = "",
        memory_backend: "LanceDbMemoryBackend | None" = None,
    ) -> dict[str, int]:
        wrote = {"facts": 0, "events": 0, "style": 0, "rules": 0, "quarantine": 0}
        lancedb_primary = memory_backend is not None and memory_backend.enabled()
        lancedb_entries: list[dict[str, Any]] = []
        plan_style_update = getattr(plan, "style_update", None)
        plan_rules_update = getattr(plan, "rules_update", None)

        if plan_style_update and plan_style_update.apply:
            existing_style = _current_qstyle(db, memory_backend if lancedb_primary else None, session_key)
            explicit_hints = _extract_explicit_style_hints(user_text)
            for raw_key, raw_value in plan_style_update.keys.items():
                key = _style_key_alias(raw_key)
                if not key:
                    continue
                actual_value = _clean_style_value(key, raw_value or explicit_hints.get(key) or "", user_text=user_text)
                if not actual_value:
                    continue
                current_value = str(existing_style.get(key) or "").strip()
                if current_value and _style_value_is_noise(key, actual_value):
                    continue
                if not _should_apply_style_patch_key(
                    key=key,
                    actual_value=actual_value,
                    current_value=current_value,
                    explicit_hints=explicit_hints,
                ):
                    continue
                for target_session in _explicit_targets(session_key):
                    if not lancedb_primary:
                        db.upsert_style(target_session, key, actual_value, updated_at=ts)
                    lancedb_entries.append(
                        {
                            "id": f"{target_session}:qstyle:{key}",
                            "session_key": target_session,
                            "layer": "deep",
                            "kind": "style",
                            "fact_key": f"qstyle.{key}",
                            "value": actual_value,
                            "text": actual_value,
                            "summary": actual_value,
                            "importance": 1.0,
                            "confidence": 1.0,
                            "strength": 1.0,
                            "timestamp": ts,
                        }
                    )
                wrote["style"] += 1

        if plan_rules_update and plan_rules_update.apply:
            existing_rules = _current_qrule(db, memory_backend if lancedb_primary else None, session_key)
            for key, raw_value in plan_rules_update.rules.items():
                if not key.startswith(RULE_PREFIXES):
                    continue
                actual_value = _normalize_rule_value(key, raw_value)
                if not actual_value:
                    continue
                current_value = str(existing_rules.get(key) or "").strip()
                if not _should_apply_rule_patch_key(
                    key=key,
                    actual_value=actual_value,
                    current_value=current_value,
                ):
                    continue
                for target_session in _explicit_targets(session_key):
                    if not lancedb_primary:
                        db.upsert_rule(target_session, key, actual_value, updated_at=ts)
                    lancedb_entries.append(
                        {
                            "id": f"{target_session}:qrule:{key}",
                            "session_key": target_session,
                            "layer": "deep",
                            "kind": "rule",
                            "fact_key": f"qrule.{key}",
                            "value": actual_value,
                            "text": actual_value,
                            "summary": actual_value,
                            "importance": 1.0,
                            "confidence": 1.0,
                            "strength": 1.0,
                            "timestamp": ts,
                        }
                    )
                wrote["rules"] += 1

        if memory_backend is not None and memory_backend.enabled() and lancedb_entries:
            memory_backend.ingest_memories(lancedb_entries)
        return wrote

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
                "prompt": _compact_text(prompt, limit=180),
                "recent_messages": _compact_messages(recent_messages, max_messages=1, max_chars=96),
                "style_present": bool(current_style),
                "rules_present": bool(current_rules),
                "now": now_iso,
            },
            ensure_ascii=False,
        )
        return await self._call(
            session_key=session_key,
            op="recall_plan",
            system_prompt=system,
            user_prompt=user,
            schema_model=BrainRecallPlan,
            max_tokens=self.cfg.brain.recall_max_tokens,
        )

    async def build_merge_plan(self, *, session_key: str, candidate_groups: list[dict[str, Any]]) -> tuple[BrainMergePlan, str, dict[str, Any]]:
        system = self._prompt(
            "merge_system.txt",
            "Return JSON only. Decide which memory items should merge, keep, or prune. Prefer consolidating duplicates without losing facts.",
        )
        user = json.dumps({"session_key": session_key, "candidate_groups": candidate_groups[:8]}, ensure_ascii=False)
        return await self._call(
            session_key=session_key,
            op="merge_plan",
            system_prompt=system,
            user_prompt=user,
            schema_model=BrainMergePlan,
            max_tokens=self.cfg.brain.merge_max_tokens,
        )

    async def build_audit_patch(self, *, session_key: str, text: str, reasons: list[str]) -> tuple[BrainAuditPatchPlan, str, dict[str, Any]]:
        system = self._prompt(
            "audit_patch_system.txt",
            "Return JSON only. Keep structure, patch only unsafe spans, and preserve meaning.",
        )
        user = json.dumps({"session_key": session_key, "text": text, "reasons": reasons}, ensure_ascii=False)
        return await self._call(
            session_key=session_key,
            op="audit_patch",
            system_prompt=system,
            user_prompt=user,
            schema_model=BrainAuditPatchPlan,
            max_tokens=self.cfg.brain.audit_max_tokens,
        )

    def apply_ingest_plan(
        self,
        db: MemqDB,
        *,
        session_key: str,
        plan: BrainIngestPlan,
        ts: int,
        user_text: str = "",
        assistant_text: str = "",
        style_rules_only: bool = False,
        memory_backend: "LanceDbMemoryBackend | None" = None,
    ) -> dict[str, int]:
        wrote = {"facts": 0, "events": 0, "style": 0, "rules": 0, "quarantine": 0}
        lancedb_primary = memory_backend is not None and memory_backend.enabled()
        style_fact_values: dict[str, str] = {}
        rule_fact_values: dict[str, str] = {}
        explicit_style_hints = _extract_explicit_style_hints(user_text)
        lancedb_entries: list[dict[str, Any]] = []
        event_payloads: list[dict[str, Any]] = []
        plan_facts = list(getattr(plan, "facts", []) or [])
        plan_events = list(getattr(plan, "events", []) or [])
        plan_quarantine = list(getattr(plan, "quarantine", []) or [])
        plan_style_update = getattr(plan, "style_update", None)
        plan_rules_update = getattr(plan, "rules_update", None)
        for fact in plan_facts:
            style_key = _style_key_alias(fact.fact_key)
            if style_key and fact.value:
                style_fact_values[style_key] = fact.value
            if fact.fact_key.startswith(RULE_PREFIXES) and fact.value:
                rule_fact_values[fact.fact_key] = fact.value
        if not style_rules_only:
            for item in plan_quarantine:
                db.insert_quarantine(session_key, item.raw_snippet, item.reason, item.risk)
                wrote["quarantine"] += 1
            for fact in plan_facts:
                if not fact.fact_key.startswith(FACT_KEY_PREFIXES):
                    db.insert_quarantine(session_key, fact.evidence_quote or fact.value, "unknown_fact_key", 0.8)
                    wrote["quarantine"] += 1
                    continue
                if fact.layer == "deep" and fact.confidence < 0.45:
                    layer = "surface"
                else:
                    layer = fact.layer
                if not lancedb_primary:
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
                lancedb_entries.append(
                    {
                        "id": f"{session_key}:{fact.fact_key}:{ts}:{wrote['facts']}",
                        "session_key": session_key,
                        "layer": layer,
                        "kind": "fact",
                        "fact_key": fact.fact_key,
                        "value": fact.value,
                        "text": fact.evidence_quote or fact.value,
                        "summary": f"{fact.fact_key}:{fact.value}",
                        "importance": fact.importance,
                        "confidence": fact.confidence,
                        "strength": fact.strength,
                        "timestamp": ts,
                    }
                )
                if layer == "deep" and session_key != "global" and fact.fact_key.startswith("profile."):
                    if not lancedb_primary:
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
                    lancedb_entries.append(
                        {
                            "id": f"global:{fact.fact_key}:{ts}:{wrote['facts']}",
                            "session_key": "global",
                            "layer": layer,
                            "kind": "fact",
                            "fact_key": fact.fact_key,
                            "value": fact.value,
                            "text": fact.evidence_quote or fact.value,
                            "summary": f"{fact.fact_key}:{fact.value}",
                            "importance": fact.importance,
                            "confidence": fact.confidence,
                            "strength": fact.strength,
                            "timestamp": ts,
                        }
                )
                wrote["facts"] += 1
            for event in plan_events:
                raw_event_text = ""
                actor_name = str(event.actor or "").lower()
                if actor_name == "user":
                    raw_event_text = _turn_event_text(user_text)[:400]
                elif actor_name == "assistant":
                    raw_event_text = _turn_event_text(assistant_text)[:400]
                if not raw_event_text:
                    raw_event_text = event.summary
                # When LanceDB is the memory authority, prefer raw turn events over
                # model-generated synthetic event summaries. Those summaries can stay
                # in the plan for metadata/debug purposes, but QCTX should retrieve
                # actual remembered text rather than compressed paraphrases.
                if lancedb_primary:
                    continue
                if not lancedb_primary:
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
                event_payloads.append(
                    {
                        "summary": event.summary,
                        "kind": event.kind,
                        "actor": event.actor,
                        "ts": event.ts or ts,
                        "salience": event.salience,
                        "text": raw_event_text,
                    }
                )
                lancedb_entries.append(
                    {
                        "id": f"{session_key}:event:{event.kind}:{event.ts or ts}:{wrote['events']}",
                        "session_key": session_key,
                        "layer": "surface",
                        "kind": "event",
                        "fact_key": f"event.{event.kind}.{event.actor}",
                        "value": event.summary,
                        "text": raw_event_text,
                        "summary": event.summary,
                        "importance": event.salience,
                        "confidence": 1.0,
                        "strength": event.salience,
                        "timestamp": event.ts or ts,
                    }
                )
                wrote["events"] += 1
            _append_turn_event(
                lancedb_primary=lancedb_primary,
                db=db,
                session_key=session_key,
                lancedb_entries=lancedb_entries,
                event_payloads=event_payloads,
                wrote=wrote,
                ts=ts,
                actor="user",
                text=user_text,
                kind="chat",
                salience=0.52,
            )
            _append_turn_event(
                lancedb_primary=lancedb_primary,
                db=db,
                session_key=session_key,
                lancedb_entries=lancedb_entries,
                event_payloads=event_payloads,
                wrote=wrote,
                ts=ts,
                actor="assistant",
                text=assistant_text,
                kind="chat",
                salience=0.36,
            )
            if wrote["events"] == 0:
                fallback_summary = _turn_event_text(user_text)[:160]
                if fallback_summary:
                    raw_event_text = _turn_event_text(user_text)[:400]
                    if not lancedb_primary:
                        db.insert_event(
                            session_key=session_key,
                            ts=ts,
                            actor="user",
                            kind="chat",
                            summary=fallback_summary,
                            salience=0.35,
                            keywords=[],
                            ttl_days=14,
                        )
                    event_payloads.append(
                        {
                            "summary": fallback_summary,
                            "kind": "chat",
                            "actor": "user",
                            "ts": ts,
                            "salience": 0.35,
                            "text": raw_event_text,
                        }
                    )
                    lancedb_entries.append(
                        {
                            "id": f"{session_key}:event:chat:{ts}:fallback",
                            "session_key": session_key,
                            "layer": "surface",
                            "kind": "event",
                            "fact_key": "event.chat.user",
                            "value": fallback_summary,
                            "text": raw_event_text,
                            "summary": fallback_summary,
                            "importance": 0.35,
                            "confidence": 1.0,
                            "strength": 0.35,
                            "timestamp": ts,
                        }
                    )
                    wrote["events"] += 1
        style_values_to_apply: dict[str, str] = {}
        style_requested_by_plan = bool(plan_style_update and plan_style_update.apply)
        style_facts_present = bool(style_fact_values)
        existing_style = _current_qstyle(db, memory_backend if lancedb_primary else None, session_key)
        if style_requested_by_plan:
            for key, value in plan_style_update.keys.items():
                if key not in STYLE_KEYS:
                    continue
                actual_value = _clean_style_value(key, value or style_fact_values.get(key) or "", user_text=user_text)
                if not actual_value:
                    continue
                current_value = str(existing_style.get(key) or "").strip()
                if _should_apply_style_patch_key(
                    key=key,
                    actual_value=actual_value,
                    current_value=current_value,
                    explicit_hints=explicit_style_hints,
                ):
                    style_values_to_apply[key] = actual_value
        if style_requested_by_plan or style_facts_present:
            for fact in plan_facts:
                key = _style_key_alias(fact.fact_key)
                if not key:
                    continue
                actual_value = _clean_style_value(key, fact.value, user_text=user_text)
                if not actual_value:
                    continue
                current = style_values_to_apply.get(key, "")
                if not current or _style_value_is_noise(key, current):
                    style_values_to_apply[key] = actual_value
            if style_values_to_apply:
                for key, value in explicit_style_hints.items():
                    if key not in STYLE_KEYS or not value:
                        continue
                    style_values_to_apply[key] = value
        for key, actual_value in style_values_to_apply.items():
            for target_session in _explicit_targets(session_key):
                if not lancedb_primary:
                    db.upsert_style(target_session, key, actual_value, updated_at=ts)
                lancedb_entries.append(
                    {
                        "id": f"{target_session}:qstyle:{key}",
                        "session_key": target_session,
                        "layer": "deep",
                        "kind": "style",
                        "fact_key": f"qstyle.{key}",
                        "value": actual_value,
                        "text": actual_value,
                        "summary": actual_value,
                        "importance": 1.0,
                        "confidence": 1.0,
                        "strength": 1.0,
                        "timestamp": ts,
                    }
                )
            wrote["style"] += 1
        rules_requested_by_plan = bool(plan_rules_update and plan_rules_update.apply)
        rule_facts_present = bool(rule_fact_values)
        existing_rules = _current_qrule(db, memory_backend if lancedb_primary else None, session_key)
        if rules_requested_by_plan:
            for key, value in plan_rules_update.rules.items():
                if not key.startswith(RULE_PREFIXES):
                    continue
                actual_value = _normalize_rule_value(key, value or rule_fact_values.get(key) or "")
                if not actual_value:
                    continue
                current_value = str(existing_rules.get(key) or "").strip()
                if not _should_apply_rule_patch_key(
                    key=key,
                    actual_value=actual_value,
                    current_value=current_value,
                ):
                    continue
                for target_session in _explicit_targets(session_key):
                    if not lancedb_primary:
                        db.upsert_rule(target_session, key, actual_value, updated_at=ts)
                    lancedb_entries.append(
                        {
                            "id": f"{target_session}:qrule:{key}",
                            "session_key": target_session,
                            "layer": "deep",
                            "kind": "rule",
                            "fact_key": f"qrule.{key}",
                            "value": actual_value,
                            "text": actual_value,
                            "summary": actual_value,
                            "importance": 1.0,
                            "confidence": 1.0,
                            "strength": 1.0,
                            "timestamp": ts,
                        }
                    )
                wrote["rules"] += 1
        if not style_rules_only:
            fallback_summary = " ".join(str(user_text or "").split())[:160]
            if lancedb_primary:
                lancedb_entries.extend(
                    _lancedb_digest_entries(
                        session_key=session_key,
                        ts=ts,
                        events=event_payloads,
                        fallback_summary=fallback_summary,
                    )
                )
            else:
                db.refresh_recent_digests(session_key, days=3)
                for digest in db.export_recent_digests(session_key, days=3):
                    lancedb_entries.append(
                        {
                            "id": f"{session_key}:digest:{digest['day_key']}",
                            "session_key": session_key,
                            "layer": "surface",
                            "kind": "digest",
                            "fact_key": f"digest.{digest['day_key']}",
                            "value": digest["digest_micro"],
                            "text": digest["digest_meso"] or digest["digest_micro"],
                            "summary": digest["digest_micro"],
                            "importance": 0.8,
                            "confidence": 1.0,
                            "strength": 0.8,
                            "timestamp": digest["updated_at"],
                        }
                    )
        if memory_backend is not None and memory_backend.enabled() and lancedb_entries:
            memory_backend.ingest_memories(lancedb_entries)
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
