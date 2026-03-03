from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple
import re
import time
from collections import defaultdict

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


def _split_kv(line: str) -> Tuple[str, str]:
    if "=" not in line:
        return line.strip(), ""
    k, v = line.split("=", 1)
    return k.strip(), v.strip()


def _line_group(key: str) -> str:
    if key.startswith("wm.") or key in {"convsurf", "convdeep"}:
        return "anchor"
    if key == "p.snapshot" or re.fullmatch(r"p\d+", key):
        return "profile"
    if key.startswith("t."):
        return "timeline"
    if re.fullmatch(r"s\d+", key):
        return "surface"
    if re.fullmatch(r"(d|g)\d+", key):
        return "deep"
    if re.fullmatch(r"e\d+", key):
        return "ephemeral"
    if key.startswith("meta.") or key.startswith("memory.fact_"):
        return "meta"
    return "misc"


def _pack_memctx_payload(
    *,
    base_lines: Sequence[str],
    payload_lines: Sequence[str],
    budget_tokens: int,
    intent: Dict[str, float],
    time_scoped: bool,
    query_memory_overview: bool,
) -> List[str]:
    out: List[str] = [ln for ln in base_lines if ln.strip()]
    used = sum(estimate_tokens(ln) for ln in out)
    budget = max(8, int(budget_tokens))
    remain = max(0, budget - used)
    if remain <= 0 or not payload_lines:
        return fit_budget(out, budget)

    weights = {
        "anchor": 0.55 + 0.28 * float(intent.get("state", 0.0)) + 0.24 * float(intent.get("overview", 0.0)),
        "profile": 0.35 + 1.10 * float(intent.get("profile", 0.0)),
        "timeline": 0.30 + 1.12 * float(intent.get("timeline", 0.0)),
        "surface": 0.42 + 0.72 * float(intent.get("state", 0.0)) + 0.35 * float(intent.get("overview", 0.0)),
        "deep": 0.50
        + 0.95 * float(intent.get("fact_lookup", 0.0))
        + 0.55 * float(intent.get("profile", 0.0))
        + 0.30 * float(intent.get("overview", 0.0)),
        "ephemeral": 0.12 + 0.25 * float(intent.get("state", 0.0)),
        "meta": 0.15 + 0.80 * float(intent.get("meta", 0.0)),
        "misc": 0.22,
    }
    if time_scoped:
        weights["timeline"] *= 1.45
        weights["anchor"] *= 0.78
    if float(intent.get("profile", 0.0)) >= 0.45:
        weights["profile"] *= 1.30
    if float(intent.get("fact_lookup", 0.0)) >= 0.45:
        weights["deep"] *= 1.25
    if query_memory_overview:
        weights["surface"] *= 1.18
        weights["deep"] *= 1.18
        weights["timeline"] *= 1.12

    sum_w = sum(max(0.01, float(v)) for v in weights.values())
    token_targets: Dict[str, int] = {
        g: max(0, int(remain * (max(0.01, float(w)) / sum_w))) for g, w in weights.items()
    }
    if time_scoped:
        token_targets["timeline"] = max(token_targets["timeline"], int(remain * 0.35))
    if float(intent.get("profile", 0.0)) >= 0.55:
        token_targets["profile"] = max(token_targets["profile"], int(remain * 0.24))
    if float(intent.get("meta", 0.0)) >= 0.60:
        token_targets["meta"] = max(token_targets["meta"], int(remain * 0.12))

    candidates: List[Dict[str, Any]] = []
    for idx, line in enumerate(payload_lines):
        ln = (line or "").strip()
        if not ln:
            continue
        key, value = _split_kv(ln)
        if not key:
            continue
        group = _line_group(key)
        cost = estimate_tokens(ln)
        val_toks = tokenize_lexical(value)
        utility = float(weights.get(group, 0.2))
        if key in {"wm.surf", "p.snapshot", "t.recent"}:
            utility += 0.20
        if key.startswith("t.digest"):
            utility += 0.35
        elif key.startswith("t.ev"):
            utility += 0.20
        elif key.startswith("d"):
            utility += 0.16
        elif key.startswith("s"):
            utility += 0.08
        candidates.append(
            {
                "idx": idx,
                "line": ln,
                "key": key,
                "group": group,
                "cost": max(1, int(cost)),
                "utility": max(0.05, utility),
                "tokens": val_toks,
            }
        )

    selected: List[Dict[str, Any]] = []
    selected_ids: set[int] = set()
    selected_token_sets: List[set[str]] = []
    used_by_group = defaultdict(int)

    def _can_fit(cost: int) -> bool:
        nonlocal remain
        return cost <= remain

    def _select(c: Dict[str, Any]) -> bool:
        nonlocal remain
        if c["idx"] in selected_ids:
            return False
        if not _can_fit(int(c["cost"])):
            return False
        selected_ids.add(int(c["idx"]))
        selected.append(c)
        remain -= int(c["cost"])
        used_by_group[str(c["group"])] += int(c["cost"])
        if c["tokens"]:
            selected_token_sets.append(set(c["tokens"]))
        return True

    must_keys = {"wm.surf", "p.snapshot"}
    if time_scoped:
        must_keys.add("t.range")
    else:
        must_keys.add("t.recent")
    for c in sorted(candidates, key=lambda x: x["idx"]):
        if c["key"] in must_keys:
            _select(c)

    while remain > 0:
        best = None
        best_score = -1.0
        for c in candidates:
            if c["idx"] in selected_ids:
                continue
            cost = int(c["cost"])
            if cost > remain:
                continue
            group = str(c["group"])
            target = max(1, int(token_targets.get(group, 0)))
            scarcity = 1.15 if used_by_group[group] < target else 0.88
            novelty = 1.0
            tks = c["tokens"]
            if tks and selected_token_sets:
                max_j = 0.0
                for prev in selected_token_sets:
                    inter = len(tks & prev)
                    union = max(1, len(tks | prev))
                    max_j = max(max_j, float(inter) / float(union))
                if max_j >= 0.88:
                    novelty = 0.38
                elif max_j >= 0.76:
                    novelty = 0.62
            score = (float(c["utility"]) / float(cost)) * scarcity * novelty
            if score > best_score:
                best = c
                best_score = score
        if best is None or best_score <= 0.0:
            break
        _select(best)

    selected_groups = {str(c["group"]) for c in selected}
    if not ({"surface", "deep", "timeline"} & selected_groups):
        for pref_group in ("timeline", "deep", "surface"):
            chosen = None
            for c in candidates:
                if c["idx"] in selected_ids or str(c["group"]) != pref_group:
                    continue
                if _can_fit(int(c["cost"])):
                    chosen = c
                    break
            if chosen is not None:
                _select(chosen)
                break

    selected.sort(key=lambda x: x["idx"])
    out.extend([str(c["line"]) for c in selected])
    return fit_budget(out, budget)


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
        return str(db.get_profile_snapshot(session_key, max_parts=8) or "")[:220]

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

    def _push_timeline_block() -> None:
        if not timeline_range:
            return
        digest_day_cap = 2 if time_scoped else 4
        digest_item_limit = 120 if time_scoped else 180
        event_cap = 6 if time_scoped else 4
        event_item_limit = 96 if time_scoped else 120
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
            compact = _clean_summary(str(r["compact_text"] or "").replace("\n", " | "), digest_item_limit)
            if not compact:
                continue
            digest_parts.append(f"{day_key}:{compact}")
            if len(digest_parts) >= digest_day_cap:
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
            if ev_count >= event_cap:
                break
            day_key = str(r["day_key"] or "")
            actor = str(r["actor"] or "assistant")
            kind = str(r["kind"] or "chat")
            summary = _clean_summary(str(r["summary"] or ""), event_item_limit)
            if not summary:
                continue
            sig = summary.lower()
            if sig in ev_seen:
                continue
            ev_seen.add(sig)
            ev_count += 1
            _push_kv(lines, f"t.ev{ev_count}", f"{day_key} {actor}/{kind}: {summary}")

    # For explicit time-scoped queries, prioritize timeline recall before anchors.
    if time_scoped:
        _push_timeline_block()

    # Always-on anchors keep conversational continuity even when query routing misses.
    # In explicit time-scoped mode keep anchors shorter so t.* survives budget trimming.
    anchor_limit = 80 if time_scoped else 120
    convsurf_anchor = _clean_summary((db.get_conv_summary(session_key, "surface_only") or "").replace(chr(10), " | "), 90 if time_scoped else 110)
    convdeep_anchor = _clean_summary((db.get_conv_summary(session_key, "deep") or "").replace(chr(10), " | "), 90 if time_scoped else 110)
    profile_anchor = _clean_summary(_profile_snapshot_line(), anchor_limit)
    recent_anchor = _clean_summary(_recent_timeline_line(), anchor_limit)
    _push_kv(lines, "wm.surf", convsurf_anchor or "none", dedupe_by_value=False)
    _push_kv(lines, "wm.deep", convdeep_anchor or "none", dedupe_by_value=False)
    _push_kv(lines, "p.snapshot", profile_anchor or "unknown", dedupe_by_value=False)
    _push_kv(lines, "t.recent", recent_anchor or "none", dedupe_by_value=False)

    # Non-explicit timeline prompts can still attach compact t.* context after anchors.
    if timeline_range and not time_scoped:
        _push_timeline_block()

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
    durable_rows = db.list_memory_items("deep", "global", limit=256)
    g_count = 0
    g_limit = 1 if (query_memory_overview or profile_query or bool(q_fact_keys) or d_count == 0) else 0
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
            if (q_fact_keys & row_keys) == set() and rel < 0.08:
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
    elif d_count == 0 and q_fact_keys and g_count == 0:
        _push_kv(lines, "memory.fact_status", "weak_or_missing", dedupe_by_value=False)

    # Mark ephemeral only for directly relevant prompts.
    if re.search(r"(直近|recent|temporary|一時|ephemeral)", prompt, re.IGNORECASE):
        eph = db.list_memory_items("ephemeral", session_key, limit=2)
        for idx, row in enumerate(eph):
            summary = str(row["summary"])[:120].replace("\n", " ")
            if _lex_rel(summary) > 0.0:
                _push_kv(lines, f"e{idx+1}", summary)

    base_lines = lines[:2]
    payload_lines = lines[2:]
    packed = _pack_memctx_payload(
        base_lines=base_lines,
        payload_lines=payload_lines,
        budget_tokens=budget_tokens,
        intent=intent,
        time_scoped=time_scoped,
        query_memory_overview=query_memory_overview,
    )
    return "\n".join(packed)
