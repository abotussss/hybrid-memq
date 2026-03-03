from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import re
import time

from .db import MemqDB
from .fact_keys import infer_query_fact_keys
from .intent import infer_intent
from .rules import extract_allowed_languages_from_rules
from .style import style_profile_lines
from .text_sanitize import strip_memq_blocks
from .timeline import day_key_from_ts, detect_timeline_range
from .tokens import tokenize_lexical


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def fit_budget(lines: Sequence[str], budget: int) -> List[str]:
    out: List[str] = []
    used = 0
    for line in lines:
        ln = line.strip()
        if not ln:
            continue
        cost = estimate_tokens(ln)
        if used + cost > budget:
            break
        out.append(ln)
        used += cost
    return out


def build_memrules(db: MemqDB, budget_tokens: int) -> str:
    lines: List[str] = [f"budget_tokens={budget_tokens}"]
    # Reserve precedence hints early so they survive budget trimming.
    style = db.get_style_profile()
    if style.get("persona"):
        lines.append("identity.precedence=memstyle")
        lines.append("identity.no_generic_assistant_label=true")

    rules = db.list_rules()
    for row in rules:
        body = str(row["body"])
        lines.append(body)

    # derive language allowlist if rule missing
    has_lang = any(x.startswith("language.allowed=") for x in lines)
    if not has_lang:
        langs = extract_allowed_languages_from_rules(db)
        lines.append(f"language.allowed={','.join(langs)}")

    # de-duplicate while preserving order
    seen = set()
    deduped: List[str] = []
    for ln in lines:
        key = ln.strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    lines = fit_budget(deduped, budget_tokens)
    return "\n".join(lines)


def build_memstyle(db: MemqDB, budget_tokens: int) -> str:
    lines = [f"budget_tokens={budget_tokens}"]
    lines.extend(style_profile_lines(db))
    lines = fit_budget(lines, budget_tokens)
    return "\n".join(lines)


def build_memctx(
    *,
    db: MemqDB,
    session_key: str,
    prompt: str,
    surface: Sequence[Dict[str, Any]],
    deep: Sequence[Dict[str, Any]],
    budget_tokens: int,
) -> str:
    rule_like = re.compile(r"(language\\.allowed=|security\\.|procedure\\.|compliance\\.|rules\\.)", re.IGNORECASE)
    style_like = re.compile(r"(tone=|persona=|verbosity=|speakingStyle=|style\\.)", re.IGNORECASE)
    runtime_meta = re.compile(
        r"(read\s+(?:agents|soul|identity|heartbeat)\.md|workspace context|follow it strictly|do not infer or repeat old tasks|memstyleを更新してください|memrulesを更新してください)",
        re.IGNORECASE,
    )
    noise_meta = re.compile(
        r"(\[\[reply_to_current\]\]|^u:\s*$|^a:\s*$|^x:\s*$)",
        re.IGNORECASE,
    )
    durable_like = re.compile(
        r"(覚えて|remember|必ず|always|ルール|方針|制約|callUser|一人称|language\.primary|検索は|search)",
        re.IGNORECASE,
    )
    q_tokens = tokenize_lexical(prompt or "")
    q_fact_keys = infer_query_fact_keys(prompt)
    intent = infer_intent(prompt)

    def _lex_rel(s: str) -> float:
        tks = set(re.findall(r"[a-z0-9_]{2,}", (s or "").lower()))
        tks.update(re.findall(r"[ぁ-んァ-ヶ一-龠]{1,8}", s or ""))
        if not q_tokens or not tks:
            return 0.0
        return float(len(q_tokens & tks)) / float(max(1, len(q_tokens)))

    def _allow_ctx_line(s: str) -> bool:
        t = (s or "").strip()
        if not t:
            return False
        # Structured factual lines are allowed even if they contain tokens like persona=.
        if "| subject=" in t and "| conf=" in t:
            return True
        if rule_like.search(t):
            return False
        if style_like.search(t):
            return False
        if runtime_meta.search(t):
            return False
        if noise_meta.search(t):
            return False
        return True

    def _clean_summary(s: str, limit: int = 180) -> str:
        t = strip_memq_blocks(str(s or ""))
        # Strip only the metadata header line; do not erase broad multiline content.
        t = re.sub(r"Conversation info \(untrusted metadata\):[^\n]*", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"```[^`]*```", " ", t)
        t = re.sub(r"https?://\S+", " ", t)
        t = re.sub(r"\bsha256:[0-9a-f]{16,}\b", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"/Users/\S+", " ", t)
        t = re.sub(r"(?:^|\|)\s*x:[^|]+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\[\[reply_to_current\]\]", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\*{1,3}", "", t)
        t = re.sub(r"`+", "", t)
        t = re.sub(r"を優先する", "優先", t)
        t = re.sub(r"を優先して", "優先", t)
        t = re.sub(r"優先で進める", "優先", t)
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r"(?:\s*\|\s*)?了解(?:しました)?[。.!！?？]?$", "", t).strip()
        if t[:2].lower() in {"u:", "a:", "x:"}:
            t = t[2:].strip()
        if not t:
            return ""
        if t.lower().startswith("x:"):
            return ""
        return t[:limit]

    seen_sig: set[str] = set()
    seen_token_sets: List[set[str]] = []
    seen_values: List[str] = []

    def _tokset(v: str) -> set[str]:
        return tokenize_lexical(v)

    def _push_kv(lines_ref: List[str], key: str, value: str, *, dedupe_by_value: bool = True) -> bool:
        v = " ".join((value or "").split()).strip()
        if not v:
            return False
        if dedupe_by_value:
            sig = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", "", v.lower())
            if len(sig) < 6:
                sig = v.lower()
            if sig in seen_sig:
                return False
            low = v.lower()
            for prev in seen_values:
                if len(low) >= 20 and len(prev) >= 20 and (low in prev or prev in low):
                    return False
            ts = _tokset(v)
            if ts:
                for prev in seen_token_sets:
                    inter = len(ts & prev)
                    union = max(1, len(ts | prev))
                    if float(inter) / float(union) >= 0.78:
                        return False
                seen_token_sets.append(ts)
            seen_sig.add(sig)
            seen_values.append(low)
        lines_ref.append(f"{key}={v}")
        return True

    def _profile_snapshot_line() -> str:
        parts: List[str] = []
        style = db.get_style_profile()
        for k in ("callUser", "firstPerson", "persona", "tone", "verbosity"):
            v = str(style.get(k) or "").strip()
            if not v:
                continue
            parts.append(f"{k}:{v}")
            if len(parts) >= 3:
                break

        pref = db.get_preference_profile()
        for k in ("language.primary", "policy.retention.default"):
            pv = pref.get(k) or {}
            v = str(pv.get("value") or "").strip()
            conf = float(pv.get("confidence", 0.0) or 0.0)
            if not v or conf < 0.55:
                continue
            parts.append(f"{k}:{v}")
            if len(parts) >= 4:
                break

        key_label = {
            "profile.family": "family",
            "profile.family.spouse": "spouse",
            "profile.family.pet": "pet",
            "profile.identity.call_user": "callUser",
            "profile.identity.first_person": "firstPerson",
            "profile.persona.role": "persona",
            "profile.persona.tone": "tone",
        }
        fact_rows = db.fetch_deep_items_by_fact_keys(
            session_key=session_key,
            fact_keys=list(key_label.keys()),
            limit=20,
            include_global=True,
        )
        seen_labels = set()
        for row in fact_rows:
            try:
                tags = json.loads(str(row["tags"] or "{}"))
            except Exception:
                tags = {}
            fact = tags.get("fact") if isinstance(tags, dict) else {}
            if not isinstance(fact, dict):
                continue
            fk = str(fact.get("fact_key") or "")
            label = key_label.get(fk)
            if not label or label in seen_labels:
                continue
            val = str(fact.get("value") or "").strip()
            if not val:
                continue
            seen_labels.add(label)
            parts.append(f"{label}:{val[:40]}")
            if len(parts) >= 6:
                break
        return " | ".join(parts)[:220]

    def _recent_timeline_line() -> str:
        now_ts = int(time.time())
        end_day = day_key_from_ts(now_ts)
        start_day = day_key_from_ts(now_ts - 2 * 86400)
        rows = db.list_daily_digests_range(
            session_key=session_key,
            start_day=start_day,
            end_day=end_day,
            scope="session",
            limit=6,
            include_global=True,
        )
        parts: List[str] = []
        seen_days = set()
        for r in rows:
            dk = str(r["day_key"] or "")
            if not dk or dk in seen_days:
                continue
            seen_days.add(dk)
            compact = _clean_summary(str(r["compact_text"] or "").replace("\n", " | "), 120)
            if not compact:
                continue
            parts.append(f"{dk}:{compact}")
            if len(parts) >= 2:
                break
        if parts:
            return " || ".join(parts)[:220]
        ev = db.list_events_range(
            session_key=session_key,
            start_day=start_day,
            end_day=end_day,
            limit=16,
            include_global=True,
        )
        ev_parts: List[str] = []
        for r in ev:
            summary = _clean_summary(str(r["summary"] or ""), 90)
            if not summary:
                continue
            dk = str(r["day_key"] or "")
            kind = str(r["kind"] or "chat")
            ev_parts.append(f"{dk}:{kind}:{summary}")
            if len(ev_parts) >= 2:
                break
        return " || ".join(ev_parts)[:220]

    lines: List[str] = [f"budget_tokens={budget_tokens}", f"q={prompt[:96].replace(chr(10), ' ')}"]
    timeline_range = detect_timeline_range(prompt)
    time_scoped = bool(timeline_range and timeline_range.explicit)
    query_memory_overview = bool(
        intent["overview"] >= 0.55
        or re.search(r"(記憶|覚えてる|これまで|要点|summary|what.*remember|memory overview)", prompt, re.IGNORECASE)
    )
    need_meta = bool(intent["meta"] >= 0.60 or re.search(r"(件数|count|pool|stats?)", prompt, re.IGNORECASE))
    # Tiny observability hints to avoid "memory is only 1-2 lines" misconceptions.
    surface_pool = len(db.list_memory_items("surface", session_key, limit=5000))
    deep_pool = len(db.list_memory_items("deep", session_key, limit=5000))
    if need_meta:
        _push_kv(lines, "meta.surface_pool", str(surface_pool), dedupe_by_value=False)
        _push_kv(lines, "meta.deep_pool", str(deep_pool), dedupe_by_value=False)
        deep_verified = len([x for x in deep if bool(x.get("verification_ok", True))])
        _push_kv(lines, "meta.deep_verified", str(deep_verified), dedupe_by_value=False)

    # Always-on anchors keep conversational continuity even when query routing misses.
    convsurf_anchor = _clean_summary((db.get_conv_summary(session_key, "surface_only") or "").replace(chr(10), " | "), 110)
    convdeep_anchor = _clean_summary((db.get_conv_summary(session_key, "deep") or "").replace(chr(10), " | "), 110)
    profile_anchor = _clean_summary(_profile_snapshot_line(), 120)
    recent_anchor = _clean_summary(_recent_timeline_line(), 120)
    _push_kv(lines, "wm.surf", convsurf_anchor or "none", dedupe_by_value=False)
    _push_kv(lines, "wm.deep", convdeep_anchor or "none", dedupe_by_value=False)
    _push_kv(lines, "p.snapshot", profile_anchor or "unknown", dedupe_by_value=False)
    _push_kv(lines, "t.recent", recent_anchor or "none", dedupe_by_value=False)

    # Timeline / episodic context for vague temporal prompts (yesterday/recent/etc).
    if timeline_range:
        _push_kv(lines, "t.range", f"{timeline_range.start_day}..{timeline_range.end_day}", dedupe_by_value=False)
        _push_kv(lines, "t.label", timeline_range.label, dedupe_by_value=False)
        dg_rows = db.list_daily_digests_range(
            session_key=session_key,
            start_day=timeline_range.start_day,
            end_day=timeline_range.end_day,
            scope="session",
            limit=14,
            include_global=True,
        )
        digest_parts: List[str] = []
        seen_days = set()
        for r in dg_rows:
            day_key = str(r["day_key"] or "")
            if not day_key or day_key in seen_days:
                continue
            seen_days.add(day_key)
            compact = _clean_summary(str(r["compact_text"] or "").replace("\n", " | "), 180)
            if not compact:
                continue
            digest_parts.append(f"{day_key}:{compact}")
            if len(digest_parts) >= 4:
                break
        if digest_parts:
            _push_kv(lines, "t.digest", " || ".join(digest_parts), dedupe_by_value=False)

        ev_rows = db.list_events_range(
            session_key=session_key,
            start_day=timeline_range.start_day,
            end_day=timeline_range.end_day,
            limit=64,
            include_global=True,
        )
        ev_count = 0
        ev_seen = set()
        for r in ev_rows:
            if ev_count >= 4:
                break
            day_key = str(r["day_key"] or "")
            actor = str(r["actor"] or "assistant")
            kind = str(r["kind"] or "chat")
            summary = _clean_summary(str(r["summary"] or ""), 120)
            if not summary:
                continue
            sig = summary.lower()
            if sig in ev_seen:
                continue
            ev_seen.add(sig)
            ev_count += 1
            _push_kv(lines, f"t.ev{ev_count}", f"{day_key} {actor}/{kind}: {summary}")

    # Stable profile hints (non-style/rule) for deterministic long-horizon behavior.
    profile = db.get_preference_profile()
    p_count = 0
    for k in ("language.primary", "policy.retention.default", "policy.ttl.default_days"):
        pv = profile.get(k)
        if not pv:
            continue
        if float(pv.get("confidence", 0.0)) < 0.55:
            continue
        p_count += 1
        _push_kv(lines, f"p{p_count}", f"{k}:{pv.get('value')}", dedupe_by_value=False)
        if p_count >= 3:
            break

    # Keep surface/deep memory facts before long summaries so they survive budget trimming.
    s_count = 0
    if q_fact_keys:
        # Keep at least one surface clue so profile/fact questions don't collapse
        # into "no memory" when deep verification gets strict.
        s_limit = 1
    else:
        s_limit = 1 if query_memory_overview else 2
    for item in surface[:12]:
        if s_count >= s_limit:
            break
        summary = _clean_summary(item.get("summary", ""), 140)
        rel = _lex_rel(summary)
        if _allow_ctx_line(summary) and (rel >= 0.10 or (query_memory_overview and durable_like.search(summary))):
            s_count += 1
            _push_kv(lines, f"s{s_count}", summary)

    d_count = 0
    profile_query = intent["profile"] >= 0.45
    if query_memory_overview:
        d_limit = 3
    else:
        d_limit = 4 if len(q_fact_keys) >= 3 else (3 if len(q_fact_keys) >= 2 else 2)
    used_deep_fact_keys: set[str] = set()
    for item in deep[:16]:
        if d_count >= d_limit:
            break
        summary = _clean_summary(item.get("summary", ""), 140)
        rel = _lex_rel(summary)
        key_overlap = int(item.get("key_overlap", 0))
        tag_overlap = int(item.get("tag_overlap", 0))
        row_keys = set(item.get("fact_keys") or [])
        verification_ok = bool(item.get("verification_ok", True))
        verification_score = float(item.get("verification_score", 0.0) or 0.0)
        fact_conf = float(item.get("fact_confidence", 0.0) or 0.0)
        fact_ts = int(item.get("fact_ts", 0) or 0)
        src = str(item.get("source", "") or "")
        overlap_keys = row_keys & q_fact_keys if q_fact_keys else set()
        if overlap_keys and overlap_keys.issubset(used_deep_fact_keys):
            continue
        has_intent_match = bool(q_fact_keys) and tag_overlap > 0
        # Pre-response verification gate:
        # for intented factual recalls, avoid low-confidence / weak-evidence deep rows.
        weak_rel_gate = 0.08 if profile_query else 0.18
        if bool(q_fact_keys) and not verification_ok and not has_intent_match and rel < weak_rel_gate:
            continue
        verify_gate = 0.35 if profile_query else 0.46
        if bool(q_fact_keys) and has_intent_match and verification_score < verify_gate:
            continue
        no_tag_rel_gate = 0.06 if profile_query else 0.12
        if bool(q_fact_keys) and not has_intent_match and rel < no_tag_rel_gate:
            continue
        if _allow_ctx_line(summary) and (rel >= 0.05 or durable_like.search(summary) or has_intent_match):
            if has_intent_match:
                ts_s = time.strftime("%Y-%m-%d", time.gmtime(fact_ts)) if fact_ts > 0 else "na"
                if "src=" not in summary:
                    summary = f"{summary} | src={src or 'na'} | ts={ts_s} | conf={max(fact_conf, verification_score):.2f}"
            if not verification_ok and "conf=" not in summary:
                summary = f"{summary} | conf={verification_score:.2f}"
            d_count += 1
            _push_kv(lines, f"d{d_count}", summary)
            if overlap_keys:
                used_deep_fact_keys.update(overlap_keys)
    if d_count == 0 and len(deep) > 0:
        for item in deep[:16]:
            fallback = _clean_summary(item.get("summary", ""), 140)
            key_overlap = int(item.get("key_overlap", 0))
            tag_overlap = int(item.get("tag_overlap", 0))
            verification_ok = bool(item.get("verification_ok", True))
            verification_score = float(item.get("verification_score", 0.0) or 0.0)
            fact_conf = float(item.get("fact_confidence", 0.0) or 0.0)
            fact_ts = int(item.get("fact_ts", 0) or 0)
            src = str(item.get("source", "") or "")
            if bool(q_fact_keys) and tag_overlap == 0 and not profile_query:
                continue
            if bool(q_fact_keys) and not verification_ok and tag_overlap == 0 and not profile_query:
                continue
            if fallback and _allow_ctx_line(fallback):
                if bool(q_fact_keys):
                    ts_s = time.strftime("%Y-%m-%d", time.gmtime(fact_ts)) if fact_ts > 0 else "na"
                    if "src=" not in fallback:
                        fallback = f"{fallback} | src={src or 'na'} | ts={ts_s} | conf={max(fact_conf, verification_score):.2f}"
                if not verification_ok and "conf=" not in fallback:
                    fallback = f"{fallback} | conf={verification_score:.2f}"
                _push_kv(lines, "d1", fallback)
                d_count = 1
                break

    # Always carry a tiny slice of durable global memory so long-term identity/preferences survive.
    durable_rows = db.list_memory_items("deep", session_key, limit=128)
    g_count = 0
    g_limit = 1 if query_memory_overview else 0
    for row in durable_rows:
        if g_count >= g_limit:
            break
        if str(row["session_key"]) != "global":
            continue
        try:
            tags = json.loads(str(row["tags"] or "{}"))
        except Exception:
            tags = {}
        kind = str(tags.get("kind", ""))
        # Keep global lines strictly structured/fact-bearing to avoid free-text drift.
        if kind not in {"durable_global_fact", "structured_fact"}:
            continue
        clean = _clean_summary(row["summary"], 120)
        rel = _lex_rel(clean)
        if not clean or not _allow_ctx_line(clean):
            continue
        if q_fact_keys:
            try:
                tags = json.loads(str(row["tags"] or "{}"))
            except Exception:
                tags = {}
            row_keys = set(tags.get("fact_keys") or [])
            if (q_fact_keys & row_keys) == set() and rel < 0.10:
                continue
        if rel < 0.05 and not durable_like.search(clean):
            continue
        g_count += 1
        _push_kv(lines, f"g{g_count}", clean)

    if time_scoped:
        _push_kv(lines, "scope.time", "explicit", dedupe_by_value=False)
    elif d_count == 0 and not q_fact_keys:
        convsurf = db.get_conv_summary(session_key, "surface_only")
        if convsurf:
            clean = _clean_summary(convsurf.replace(chr(10), " | "), 120)
            if clean and (_lex_rel(clean) >= 0.08 or query_memory_overview):
                _push_kv(lines, "convsurf", clean)

        convdeep = db.get_conv_summary(session_key, "deep")
        if convdeep:
            clean = _clean_summary(convdeep.replace(chr(10), " | "), 120)
            if clean and (_lex_rel(clean) >= 0.08 or query_memory_overview):
                _push_kv(lines, "convdeep", clean)
    elif d_count == 0 and q_fact_keys:
        _push_kv(lines, "memory.fact_status", "weak_or_missing", dedupe_by_value=False)

    # Mark ephemeral only for directly relevant prompts.
    if re.search(r"(直近|recent|temporary|一時|ephemeral)", prompt, re.IGNORECASE):
        eph = db.list_memory_items("ephemeral", session_key, limit=2)
        for idx, row in enumerate(eph):
            summary = str(row["summary"])[:120].replace("\n", " ")
            if _lex_rel(summary) > 0.0:
                _push_kv(lines, f"e{idx+1}", summary)

    lines = fit_budget(lines, budget_tokens)
    return "\n".join(lines)
