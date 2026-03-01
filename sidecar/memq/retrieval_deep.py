from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List

import numpy as np

from .db import MemqDB
from .fact_keys import infer_query_fact_keys
from .quant import dequantize, dot, from_f16_blob

NOISE_SUMMARY_RE = re.compile(
    r"(<MEM(?:RULES|STYLE|CTX)\s+v1>|\[MEM(?:RULES|STYLE|CTX)\s+v1\]|\[\[reply_to_current\]\]|read\s+(?:agents|soul|identity|heartbeat)\.md|workspace context)",
    re.IGNORECASE,
)
META_DIAGNOSTIC_RE = re.compile(
    r"(長期記憶.*(?:不足|不完全|課題|弱い)|参照できる記憶コンテキスト.*不足|取り扱い不足|memory\s+context.*insufficient|OpenClawで動く.*アシスタント|I am .*assistant)",
    re.IGNORECASE,
)
RECENT_DIAGNOSTIC_RE = re.compile(
    r"(効率化されてるけど|安定参照が弱い|取り違え|課題って状態|memory.*issue)",
    re.IGNORECASE,
)
IMPERATIVE_STYLE_RE = re.compile(
    r"((?:お前|あなた|you).*(?:として振る舞|になりき|act as|roleplay)|(?:しろ|しなさい|してください)\s*$)",
    re.IGNORECASE,
)


def _is_noise_summary(text: str) -> bool:
    s = text or ""
    if not s:
        return False
    m = NOISE_SUMMARY_RE.search(s)
    # Guard against accidental empty-match regexes.
    return bool(m and m.group(0))


def _source_trust(source: str) -> float:
    s = (source or "").strip().lower()
    if s == "turn":
        return 1.0
    if s == "conv_summarize":
        return 0.75
    if s == "bootstrap":
        return 0.7
    return 0.6


def _extract_fact_confidence(row: Any, tags: Dict[str, Any]) -> float:
    fact = tags.get("fact") if isinstance(tags, dict) else {}
    if isinstance(fact, dict):
        try:
            c = float(fact.get("confidence"))
            if c > 0.0:
                return max(0.0, min(1.0, c))
        except Exception:
            pass
    gate = tags.get("gate") if isinstance(tags, dict) else {}
    if isinstance(gate, dict):
        try:
            c = float(gate.get("score"))
            if c > 0.0:
                return max(0.0, min(1.0, c))
        except Exception:
            pass
    kind = str(tags.get("kind", "")) if isinstance(tags, dict) else ""
    if kind in {"structured_fact", "durable_global_fact"}:
        return 0.82
    if kind in {"durable_global", "convdeep_global"}:
        return 0.72
    return 0.52


def _extract_fact_ts(row: Any, tags: Dict[str, Any]) -> int:
    fact = tags.get("fact") if isinstance(tags, dict) else {}
    if isinstance(fact, dict):
        try:
            ts = int(fact.get("ts"))
            if ts > 0:
                return ts
        except Exception:
            pass
    try:
        ts = int(tags.get("ts")) if isinstance(tags, dict) else 0
        if ts > 0:
            return ts
    except Exception:
        pass
    try:
        return int(row["updated_at"])
    except Exception:
        return int(__import__("time").time())


def _verification_score(
    *,
    confidence: float,
    source: str,
    fact_ts: int,
    now: float,
    key_overlap: int,
    lex: float,
) -> float:
    age_days = max(0.0, (float(now) - float(fact_ts)) / 86400.0)
    freshness = math.exp(-age_days / 120.0)
    src = _source_trust(source)
    key_bonus = 0.08 if key_overlap > 0 else 0.0
    lex_bonus = min(0.06, max(0.0, lex * 0.2))
    return max(
        0.0,
        min(1.0, 0.58 * max(0.0, min(1.0, confidence)) + 0.26 * freshness + 0.16 * src + key_bonus + lex_bonus),
    )


def _row_fact_keys(row: Any, summary: str) -> set[str]:
    keys: set[str] = set()
    try:
        tags = json.loads(str(row["tags"] or "{}"))
    except Exception:
        tags = {}
    for k in tags.get("fact_keys") or []:
        if k:
            keys.add(str(k))
    s = summary or ""
    if re.search(r"(家族|奥さま|妻|夫|子ども|息子|娘|犬|猫|ペット|family|wife|husband|pet)", s, re.IGNORECASE):
        keys.add("profile.family")
    if re.search(r"(妻|奥さま|夫|husband|wife)", s, re.IGNORECASE):
        keys.add("profile.family.spouse")
    if re.search(r"(犬|猫|ペット|愛犬|dog|cat|pet)", s, re.IGNORECASE):
        keys.add("profile.family.pet")
    if re.search(r"(子ども|子供|息子|娘|child|son|daughter)", s, re.IGNORECASE):
        keys.add("profile.family.child")
    if re.search(r"(人格|persona|口調|tone|speaking style|callUser|firstPerson)", s, re.IGNORECASE):
        keys.add("profile.persona")
    if re.search(r"(persona=|人格:|role=)", s, re.IGNORECASE):
        keys.add("profile.persona.role")
    if re.search(r"(呼称|callUser|ユーザー呼称)", s, re.IGNORECASE):
        keys.add("profile.identity.call_user")
    if re.search(r"(一人称|firstPerson)", s, re.IGNORECASE):
        keys.add("profile.identity.first_person")
    if re.search(r"(検索|search).*(brave|google|bing|duckduckgo)", s, re.IGNORECASE):
        keys.add("pref.search.engine")
    if re.search(r"(ルール|方針|制約|rule|policy|constraint)", s, re.IGNORECASE):
        keys.add("memory.policy")
    return keys


def _score(sim: float, importance: float, usage_count: int, age_sec: int) -> float:
    recency = math.exp(-max(0, age_sec) / 604800.0)
    # Clamp frequency impact so old frequently-hit rules do not dominate every query.
    freq = min(1.25, math.log1p(max(0, usage_count)))
    imp = min(0.85, float(importance))
    return sim + 0.34 * recency + 0.12 * freq + 0.44 * imp


def _tokenize(text: str) -> set[str]:
    s = (text or "").lower()
    out = set(re.findall(r"[a-z0-9_]{2,}", s))
    out.update(re.findall(r"[ぁ-んァ-ヶ一-龠]{1,8}", s))
    return out


def _lex_overlap(q_tokens: set[str], text: str) -> float:
    if not q_tokens:
        return 0.0
    t = _tokenize(text)
    if not t:
        return 0.0
    return float(len(q_tokens & t)) / float(len(q_tokens))


def _durable_bonus(row: Any, lex: float, summary: str) -> float:
    b = 0.0
    if str(row["session_key"]) == "global":
        b += 0.10
    try:
        tags = json.loads(str(row["tags"] or "{}"))
    except Exception:
        tags = {}
    kind = str(tags.get("kind", ""))
    if kind == "durable_global_fact":
        b += 0.55
    elif kind == "structured_fact":
        b += 0.48
    elif kind == "durable_global":
        b += 0.35
        if re.search(r"^(覚えて|remember)\b", summary or "", re.IGNORECASE):
            b -= 0.22
    elif kind == "deep_global":
        b += 0.15 if lex > 0.0 else -0.10
    elif kind == "convdeep_global":
        b += 0.03
        if lex <= 0.0:
            b -= 0.45
    return b


def _session_scope_bonus(session_key: str, row_session: str) -> float:
    if row_session == session_key:
        return 0.22
    if row_session == "global":
        return 0.10
    return -0.04


def search_deep(db: MemqDB, session_key: str, query_text: str, qvec: np.ndarray, top_k: int, bits: int, top_m: int = 200) -> List[Dict[str, Any]]:
    rows = db.list_memory_items("deep", session_key, limit=5000)
    if len(rows) < max(48, top_k * 6):
        extra = db.list_memory_items_any("deep", limit=5000)
        have = {str(r["id"]) for r in rows}
        rows.extend([r for r in extra if str(r["id"]) not in have])
    q_tokens = _tokenize(query_text)
    q_fact_keys = infer_query_fact_keys(query_text)
    is_recent_query = "memory.recent" in q_fact_keys
    now = __import__("time").time()
    scored: List[Dict[str, Any]] = []
    key_hits: List[Dict[str, Any]] = []
    for r in rows:
        summary_raw = str(r["summary"] or "")
        if _is_noise_summary(summary_raw):
            continue
        if META_DIAGNOSTIC_RE.search(summary_raw):
            continue
        if is_recent_query and RECENT_DIAGNOSTIC_RE.search(summary_raw):
            continue
        emb = None
        if r["emb_q"]:
            emb = dequantize(r["emb_q"], int(r["emb_dim"]), bits)
        elif r["emb_f16"]:
            emb = from_f16_blob(r["emb_f16"], int(r["emb_dim"]))
        if emb is None:
            continue
        sim = dot(qvec, emb)
        lex = _lex_overlap(q_tokens, summary_raw)
        age = int(now - int(r["last_access_at"]))
        row_session = str(r["session_key"])
        source = str(r["source"] or "turn")
        try:
            tags = json.loads(str(r["tags"] or "{}"))
        except Exception:
            tags = {}
        kind = str(tags.get("kind", ""))
        if q_fact_keys and kind in {"convdeep", "convdeep_global", "durable_global", "deep_global"}:
            continue
        if IMPERATIVE_STYLE_RE.search(summary_raw) and kind not in {"structured_fact", "durable_global_fact"}:
            continue
        row_keys = _row_fact_keys(r, summary_raw)
        row_tag_keys = set(tags.get("fact_keys") or [])
        key_overlap = len(q_fact_keys & row_keys)
        tag_overlap = len(q_fact_keys & row_tag_keys)
        is_structured_kind = kind in {"structured_fact", "durable_global_fact"}
        fact_conf = _extract_fact_confidence(r, tags)
        fact_ts = _extract_fact_ts(r, tags)
        verify_score = _verification_score(
            confidence=fact_conf,
            source=source,
            fact_ts=fact_ts,
            now=now,
            key_overlap=key_overlap,
            lex=lex,
        )
        verify_min = 0.55 if q_fact_keys else 0.43
        if "memory.recent" in q_fact_keys and (now - fact_ts) > 86400 * 3:
            verify_min = max(verify_min, 0.60)
        if q_fact_keys and key_overlap == 0:
            verify_min = max(verify_min, 0.58)
        verified = verify_score >= verify_min
        # Strongly bias toward tag-backed fact-key matches.
        key_bonus = (0.72 * float(tag_overlap)) + (0.12 * float(max(0, key_overlap - tag_overlap)))
        key_penalty = -0.52 if (q_fact_keys and key_overlap == 0) else 0.0
        if q_fact_keys and key_overlap == 0 and lex <= 0.01:
            # Hard-gate obvious non-matches (especially noisy global durable rows).
            if row_session == "global":
                continue
            # Keep same-session rows only when semantic similarity is reasonably high.
            if sim < 0.10:
                continue
        if q_fact_keys and tag_overlap == 0:
            # For factual prompts, require tag-backed key match.
            if not (is_structured_kind and key_overlap > 0):
                continue
        if q_fact_keys and key_overlap > 0 and tag_overlap == 0 and not is_structured_kind:
            # Heuristic text matches are weak evidence on factual prompts.
            if lex < 0.20:
                continue
        if q_fact_keys and key_overlap == 0 and source == "conv_summarize" and lex < 0.25:
            continue
        recent_bonus = 0.0
        if is_recent_query:
            if age <= 600:
                recent_bonus += 0.45
            elif age <= 1800:
                recent_bonus += 0.20
            elif age > 7200:
                recent_bonus -= 0.25
        s = (
            _score(sim, float(r["importance"]), int(r["usage_count"]), age)
            + 0.45 * lex
            + _durable_bonus(r, lex, summary_raw)
            + _session_scope_bonus(session_key, row_session)
            + key_bonus
            + key_penalty
            + recent_bonus
            + 0.24 * verify_score
        )
        if q_fact_keys and not verified:
            s -= 0.24
        rec = {
            "id": str(r["id"]),
            "score": float(s),
            "sim": float(sim),
            "lex": float(lex),
            "summary": summary_raw,
            "layer": "deep",
            "kind": kind,
            "importance": float(r["importance"]),
            "session_key": row_session,
            "fact_keys": sorted(list(row_keys)),
            "tag_keys": sorted(list(row_tag_keys)),
            "key_overlap": int(key_overlap),
            "tag_overlap": int(tag_overlap),
            "source": source,
            "fact_confidence": float(fact_conf),
            "fact_ts": int(fact_ts),
            "verification_score": float(verify_score),
            "verification_ok": bool(verified),
        }
        scored.append(rec)
        if (tag_overlap > 0 or (is_structured_kind and key_overlap > 0)) and verified:
            key_hits.append(rec)
    scored.sort(key=lambda x: x["score"], reverse=True)
    if q_fact_keys and key_hits:
        key_hits.sort(key=lambda x: x["score"], reverse=True)
        selected: List[Dict[str, Any]] = []
        used = set()
        # Ensure per-key coverage first.
        for k in sorted(q_fact_keys):
            for c in scored:
                if not bool(c.get("verification_ok", True)):
                    continue
                tag_keys = set(c.get("tag_keys") or [])
                if k not in tag_keys:
                    continue
                cid = str(c.get("id"))
                if cid in used:
                    continue
                selected.append(c)
                used.add(cid)
                break
        for c in key_hits:
            cid = str(c.get("id"))
            if cid in used:
                continue
            selected.append(c)
            used.add(cid)
        merged = selected + [x for x in scored if str(x["id"]) not in used]
        return merged[: max(1, min(top_m, len(merged), top_k))]
    return scored[: max(1, min(top_m, len(scored), top_k))]
