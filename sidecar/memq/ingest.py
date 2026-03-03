from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from .db import MemqDB
from .fact_keys import infer_text_fact_keys
from .rules import apply_rule_updates, extract_preference_events, extract_rule_updates
from .style import apply_style_updates, extract_style_updates
from .text_sanitize import strip_memq_blocks, strip_runtime_noise


INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"reveal\s+.*(api\s*key|secret|token)", re.IGNORECASE),
    re.compile(r"apiキー.*(教え|出して)", re.IGNORECASE),
]
RUNTIME_NOISE_PATTERNS = [
    re.compile(r"read\s+(?:agents|soul|identity|heartbeat)\.md", re.IGNORECASE),
    re.compile(r"workspace context", re.IGNORECASE),
    re.compile(r"follow it strictly", re.IGNORECASE),
    re.compile(r"do not infer or repeat old tasks", re.IGNORECASE),
    re.compile(r"\[\[reply_to_current\]\]", re.IGNORECASE),
]

def _sanitize_turn_text(text: str) -> str:
    raw = text or ""
    t = strip_runtime_noise(strip_memq_blocks(raw))
    # Drop known structured-injection fragments that should never become memory facts.
    bad_lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(p.search(s) for p in RUNTIME_NOISE_PATTERNS):
            continue
        if s.startswith("budget_tokens="):
            continue
        if s.startswith("language.allowed="):
            continue
        if s.startswith("security."):
            continue
        if s.startswith("compliance."):
            continue
        if s.startswith("procedure."):
            continue
        if s.startswith("[MEM"):
            continue
        bad_lines.append(s)
    out = " ".join(" ".join(bad_lines).split())
    out = re.sub(r"\[\[reply_to_current\]\]", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\*{1,3}", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    raw_norm = re.sub(r"\s+", " ", raw).strip()
    # Fail-open guard: if sanitization over-trims normal input, keep normalized original.
    if raw_norm and len(raw_norm) >= 120:
        min_len = max(12, int(len(raw_norm) * 0.08))
        if len(out) < min_len:
            out = raw_norm
    return out[:4000]


def _is_high_risk_text(text: str) -> Tuple[bool, str, float]:
    t = text or ""
    for pat in INJECTION_PATTERNS:
        if pat.search(t):
            return True, "prompt_injection_like", 0.9
    if len(t) > 6000:
        return True, "oversized_untrusted", 0.7
    return False, "", 0.0


def _surface_summary(user_text: str, assistant_text: str) -> str:
    u = " ".join((user_text or "").split())[:180]
    a = " ".join((assistant_text or "").split())[:180]
    if u and a:
        return f"u:{u} | a:{a}"
    return (u or a)[:220]


FACT_KEYWORDS = re.compile(
    r"(覚えて|remember|必ず|always|重要|important|ルール|rule|方針|policy|制約|constraint|名前|name|呼称|call me|一人称|first person|好み|prefer|目標|goal|期限|deadline)",
    re.IGNORECASE,
)
EXPLICIT_REMEMBER_RE = re.compile(
    r"(remember\s+(this|that)|please\s+remember|must|always|ルール|方針|制約|覚えて(?:おいて|ください|くれ|ね|。|！|$|:))",
    re.IGNORECASE,
)
EXPLICIT_FORGET_RE = re.compile(
    r"(do\s*not\s*remember|don't\s*remember|forget\s*this|覚えなくていい|記憶しない|忘れていい)",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    raw = re.split(r"[。．\.!！?？\n;；]+", text)
    out: List[str] = []
    for r in raw:
        s = " ".join(r.strip().split())
        if not s:
            continue
        if any(p.search(s) for p in RUNTIME_NOISE_PATTERNS):
            continue
        if len(s) < 3:
            continue
        out.append(s)
    return out


def _make_fact_summary(text: str, limit: int = 220) -> str:
    cands = _split_sentences(text)
    if not cands:
        return ""
    prioritized = [s for s in cands if FACT_KEYWORDS.search(s)]
    picked: List[str] = []
    for s in prioritized:
        picked.append(s)
        if len(picked) >= 3:
            break
    if 0 < len(picked) < 2:
        for s in cands:
            if s in picked:
                continue
            picked.append(s)
            if len(picked) >= 2:
                break
    if not picked:
        picked = cands[:2]
    out = " | ".join(picked)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


def _deep_candidate(user_text: str, assistant_text: str) -> str:
    # Deep should store compact user-grounded facts, not long assistant prose.
    u = _make_fact_summary(user_text, 260)
    if u:
        return u
    a = _make_fact_summary(assistant_text, 200)
    return a[:260]


def _contains_stable_fact_signal(text: str) -> bool:
    t = text or ""
    if re.search(r"(memstyle|スタイルを更新|口調を更新|キャラを更新|性格を更新)", t, re.IGNORECASE):
        return False
    return bool(
        re.search(r"(私の名前|僕の名前|俺の名前|呼び方|一人称|好み|苦手|目標|期限|ルール|方針|制約|性格|口調)", t)
        or re.search(r"(my name|call me|i prefer|i dislike|my goal|deadline|rule|policy|constraint|persona|style)", t, re.IGNORECASE)
    )


def _exists_summary(db: MemqDB, layer: str, session_key: str, sig: str, limit: int = 512) -> bool:
    target = sig.strip().lower()
    if not target:
        return True
    for row in db.list_memory_items(layer, session_key, limit=limit):
        if str(row["summary"]).strip().lower() == target:
            return True
    return False


def _extract_fact_keys(user_text: str, styles: Dict[str, str], rules: List[Tuple[str, str, int, str]]) -> List[str]:
    out: List[str] = []
    t = user_text or ""
    if "callUser" in styles:
        out.append("style.callUser")
    if "firstPerson" in styles:
        out.append("style.firstPerson")
    if "persona" in styles:
        out.append("style.persona")
    if "tone" in styles:
        out.append("style.tone")
    if "verbosity" in styles:
        out.append("style.verbosity")

    for rk, _rv, _prio, _kind in rules:
        if rk:
            out.append(f"rule.{rk}")

    if re.search(r"(検索|search).*(brave|google|bing|duckduckgo)", t, re.IGNORECASE):
        out.append("pref.search.engine")
    if re.search(r"(返答|回答|answer|reply).*(結論から|first|summary first)", t, re.IGNORECASE):
        out.append("rule.output.order")
    if re.search(r"(箇条書き|bullet|list format)", t, re.IGNORECASE):
        out.append("rule.output.format")
    if re.search(r"(家族|family|妻|奥さま|夫|husband|wife|子ども|息子|娘|犬|猫|ペット|pet)", t, re.IGNORECASE):
        out.append("profile.family")
        out.append("profile.family.summary")
    if re.search(r"(家族構成|family composition)", t, re.IGNORECASE):
        out.append("profile.family.summary")
    if re.search(r"(妻|奥さま|夫|husband|wife)", t, re.IGNORECASE):
        out.append("profile.family.spouse")
    if re.search(r"(犬|猫|ペット|愛犬|dog|cat|pet)", t, re.IGNORECASE):
        out.append("profile.family.pet")
    if re.search(r"(子ども|子供|息子|娘|child|son|daughter)", t, re.IGNORECASE):
        out.append("profile.family.child")
    if re.search(r"(子ども.*\d+人|\d+人.*子ども|children?\s*\d+)", t, re.IGNORECASE):
        out.append("profile.family.children_count")
    if re.search(r"((?:俺|私|僕|わたし|ぼく).{0,3}名前|my name|名前は)", t, re.IGNORECASE):
        out.append("profile.user.name")
    if re.search(r"(人格|persona|キャラ|ロール|roleplay|口調|tone|話し方|speaking style)", t, re.IGNORECASE):
        out.append("profile.persona")
        out.append("profile.persona.role")
    if re.search(r"(君は誰|あなたは誰|who are you|what are you|何者|自己紹介|identity)", t, re.IGNORECASE):
        out.append("profile.identity")
        out.append("profile.persona.role")
    if re.search(r"(呼称|呼び方|call me)", t, re.IGNORECASE):
        out.append("profile.identity.call_user")
    if re.search(r"(って呼んで|と呼んで|呼んでほしい)", t, re.IGNORECASE):
        out.append("profile.identity.call_user")
    if re.search(r"(一人称|first person)", t, re.IGNORECASE):
        out.append("profile.identity.first_person")
    if re.search(r"(10分前|直近|recent|さっき|minutes? ago)", t, re.IGNORECASE):
        out.append("memory.recent")

    uniq: List[str] = []
    seen = set()
    for k in out:
        if k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return uniq


def _norm_val(v: str, max_len: int = 48) -> str:
    return re.sub(r"\s+", " ", (v or "").strip())[:max_len]


_FORBIDDEN_PROFILE_VALUES = {
    "僕",
    "ぼく",
    "私",
    "わたし",
    "俺",
    "オレ",
    "自分",
    "me",
    "myself",
    "you",
    "yourself",
    "assistant",
    "アシスタント",
}


def _plausible_profile_value(fact_key: str, value: str) -> bool:
    v = _norm_val(value, 64)
    if not v:
        return False
    low = v.lower()
    if low in {x.lower() for x in _FORBIDDEN_PROFILE_VALUES}:
        return False
    if re.search(r"(<MEM(?:RULES|STYLE|CTX)|thinkingSignature|encrypted_content|budget_tokens=|identity\.precedence=)", v, re.IGNORECASE):
        return False
    if fact_key in {"profile.identity.first_person", "profile.identity.call_user", "profile.user.name", "profile.family.spouse", "profile.family.pet", "profile.family.child"}:
        if len(v) > 24:
            return False
        if not re.fullmatch(r"[A-Za-z0-9ぁ-んァ-ヶ一-龠ー._\-]{1,24}", v):
            return False
    if fact_key == "profile.family.summary":
        if len(v) < 2 or len(v) > 60:
            return False
        if re.search(r"(この会話|情報だけ|分からない|わからない|不足|未確認|unknown)", v, re.IGNORECASE):
            return False
    if fact_key == "profile.family.children_count":
        return bool(re.fullmatch(r"\d{1,2}", v))
    return True


def _extract_structured_facts(
    user_text: str,
    styles: Dict[str, str],
    rules: List[Tuple[str, str, int, str]],
    explicit_memory_signal: bool,
) -> List[Dict[str, Any]]:
    t = user_text or ""
    out: List[Dict[str, Any]] = []

    def add_fact(subject: str, relation: str, value: str, fact_key: str, confidence: float, stable: bool = True, ttl_days: int = 365) -> None:
        vv = _norm_val(value)
        if not vv:
            return
        if not _plausible_profile_value(fact_key, vv):
            return
        out.append(
            {
                "subject": subject,
                "relation": relation,
                "value": vv,
                "fact_key": fact_key,
                "confidence": float(confidence),
                "source": "user_msg",
                "stable": bool(stable),
                "ttl_days": int(ttl_days),
                "explicit": bool(explicit_memory_signal),
            }
        )

    spouse = re.search(r"(?:妻|奥さま|奥さん|夫|旦那|husband|wife)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})", t, re.IGNORECASE)
    if spouse:
        add_fact("user", "family.spouse", spouse.group(1), "profile.family.spouse", 0.93)

    family_summary = re.search(r"(?:家族構成|家族)\s*(?:は|が|:|：)\s*([^。\n]{2,60})", t, re.IGNORECASE)
    if family_summary:
        add_fact("user", "family.summary", family_summary.group(1), "profile.family.summary", 0.84)

    child_count = re.search(r"(?:子ども|子供|子)\s*(?:は|が|:|：)?\s*(\d{1,2})\s*人", t, re.IGNORECASE)
    if not child_count:
        child_count = re.search(r"(\d{1,2})\s*人(?:の)?\s*(?:子ども|子供|子)", t, re.IGNORECASE)
    if child_count:
        add_fact("user", "family.children_count", child_count.group(1), "profile.family.children_count", 0.84)

    pet = re.search(r"(?:愛犬|犬|猫|ペット|dog|cat|pet)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})", t, re.IGNORECASE)
    if pet:
        add_fact("user", "family.pet", pet.group(1), "profile.family.pet", 0.92)

    child = re.search(r"(?:子ども|子供|息子|娘|child|son|daughter)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})", t, re.IGNORECASE)
    if child:
        add_fact("user", "family.child", child.group(1), "profile.family.child", 0.90)

    user_name = re.search(r"(?:俺|オレ|僕|ぼく|私|わたし|自分|my)\s*(?:の)?\s*名前\s*(?:は|が|:|：)?\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})", t, re.IGNORECASE)
    if not user_name:
        user_name = re.search(r"(?:my name is)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})", t, re.IGNORECASE)
    if user_name:
        add_fact("user", "identity.user_name", user_name.group(1), "profile.user.name", 0.90)

    call_user = styles.get("callUser")
    if call_user:
        add_fact("assistant", "identity.call_user", call_user, "profile.identity.call_user", 0.96)
    else:
        m_call = re.search(r"(?:俺|ぼく|僕|私|わたし|オレ)のことは\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー]{1,24})", t, re.IGNORECASE)
        if m_call:
            add_fact("assistant", "identity.call_user", m_call.group(1), "profile.identity.call_user", 0.93)
        else:
            m_call2 = re.search(r"([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\-]{1,24})\s*(?:って|と)\s*呼んで", t, re.IGNORECASE)
            if m_call2:
                add_fact("assistant", "identity.call_user", m_call2.group(1), "profile.identity.call_user", 0.93)

    first_person = styles.get("firstPerson")
    if first_person:
        add_fact("assistant", "identity.first_person", first_person, "profile.identity.first_person", 0.95)

    persona = styles.get("persona")
    if persona:
        add_fact("assistant", "persona.role", persona, "profile.persona.role", 0.94)

    tone = styles.get("tone")
    if tone:
        add_fact("assistant", "persona.tone", tone, "profile.persona.tone", 0.86)

    m_engine = re.search(r"(?:検索|search).*(brave|google|bing|duckduckgo)", t, re.IGNORECASE)
    if m_engine:
        add_fact("user", "preference.search_engine", m_engine.group(1).lower(), "pref.search.engine", 0.90)

    if re.search(r"(結論から|summary first|first then details)", t, re.IGNORECASE):
        add_fact("assistant", "rule.output_order", "summary_first", "rule.output.order", 0.88)
    if re.search(r"(箇条書き|bullet|list format)", t, re.IGNORECASE):
        add_fact("assistant", "rule.output_format", "bullets", "rule.output.format", 0.86)

    # Rule updates from parser are also durable structured memory.
    for rk, rv, _p, _kind in rules:
        if not rk:
            continue
        add_fact("assistant", f"rule.{rk}", rv, f"rule.{rk}", 0.85)

    uniq: List[Dict[str, Any]] = []
    seen = set()
    for f in out:
        sig = f"{f['fact_key']}::{f['value']}".lower()
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(f)
    return uniq


def _fact_novelty(db: MemqDB, session_key: str, fact_key: str, value: str, limit: int = 800) -> Tuple[float, bool]:
    value_l = (value or "").strip().lower()
    rows = db.list_memory_items("deep", session_key, limit=limit)
    same_value = False
    same_key_diff = False
    for r in rows:
        try:
            tags = json.loads(str(r["tags"] or "{}"))
        except Exception:
            tags = {}
        f = tags.get("fact") or {}
        k = str(f.get("fact_key") or "")
        v = str(f.get("value") or "").strip().lower()
        if k != fact_key:
            continue
        if v and v == value_l:
            same_value = True
            break
        same_key_diff = True
    if same_value:
        return 0.0, True
    if same_key_diff:
        return 0.35, False
    return 1.0, False


def _fact_repetition(db: MemqDB, session_key: str, fact_key: str, limit: int = 800) -> float:
    if not fact_key:
        return 0.0
    rows = db.list_memory_items("deep", session_key, limit=limit)
    n = 0
    for r in rows:
        try:
            tags = json.loads(str(r["tags"] or "{}"))
        except Exception:
            tags = {}
        fk = set(tags.get("fact_keys") or [])
        if fact_key in fk:
            n += 1
    return max(0.0, min(1.0, float(n) / 3.0))


def _subject_match(user_text: str, fact: Dict[str, Any]) -> float:
    t = user_text or ""
    subject = str(fact.get("subject") or "user")
    rel = str(fact.get("relation") or "")
    if subject == "user":
        if re.search(r"(私|わたし|僕|ぼく|俺|オレ|my|me|I\\b)", t, re.IGNORECASE):
            return 1.0
        return 0.55
    if subject == "assistant":
        if re.search(r"(呼称|呼び方|一人称|口調|人格|キャラ|call me|first person|tone|persona)", t, re.IGNORECASE):
            return 1.0
        if rel.startswith("rule."):
            return 0.9
        return 0.65
    return 0.6


def _write_gate_score(user_text: str, fact: Dict[str, Any], novelty: float, repetition: float, subj_match: float) -> Tuple[float, float]:
    fact_key = str(fact.get("fact_key") or "")
    explicit = 1.0 if EXPLICIT_REMEMBER_RE.search(user_text or "") else 0.0
    utility = 1.0 if bool(fact.get("stable", True)) else 0.55
    stability = 1.0 if fact_key.startswith(("profile.", "pref.", "rule.")) else 0.6
    redundancy = 1.0 - max(0.0, min(1.0, novelty))
    score = (
        0.28 * utility
        + 0.22 * novelty
        + 0.18 * stability
        + 0.22 * explicit
        + 0.14 * repetition
        + 0.16 * subj_match
        - 0.16 * redundancy
    )
    threshold = 0.52 if fact_key.startswith(("profile.", "pref.", "rule.")) else 0.68
    return float(score), float(threshold)


def _structured_fact_summary(f: Dict[str, Any]) -> str:
    rel = str(f.get("relation") or "")
    val = str(f.get("value") or "")
    subj = str(f.get("subject") or "user")
    conf = float(f.get("confidence", 0.8))
    src = str(f.get("source") or "user_msg")
    ttl_days = int(f.get("ttl_days") or 365)
    if rel == "family.spouse":
        core = f"家族: 妻={val}"
    elif rel == "family.summary":
        core = f"家族構成: {val}"
    elif rel == "family.children_count":
        core = f"家族: 子ども人数={val}"
    elif rel == "family.pet":
        core = f"家族: ペット={val}"
    elif rel == "family.child":
        core = f"家族: 子ども={val}"
    elif rel == "identity.user_name":
        core = f"プロフィール: 名前={val}"
    elif rel == "identity.call_user":
        core = f"呼称: ユーザー呼称={val}"
    elif rel == "identity.first_person":
        core = f"一人称: {val}"
    elif rel == "persona.role":
        core = f"人格: persona={val}"
    elif rel == "preference.search_engine":
        core = f"設定: 検索エンジン={val}"
    elif rel == "rule.output_order":
        core = f"ルール: 出力順={val}"
    elif rel == "rule.output_format":
        core = f"ルール: 出力形式={val}"
    else:
        core = f"fact: {rel}={val}"
    return f"{core} | subject={subj} | conf={conf:.2f} | src={src} | ttl={ttl_days}d"[:220]


def _policy_ttl_days(db: MemqDB, default_days: int = 45) -> int:
    profile = db.get_memory_policy_profile()
    try:
        raw = str((profile.get("ttl.default_days") or {}).get("value") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return int(default_days)
    try:
        v = int(float(raw))
    except Exception:
        return int(default_days)
    return max(1, min(3650, v))


def _event_importance(
    *,
    explicit_memory_signal: bool,
    deep_signal: bool,
    auto_deep_signal: bool,
    structured_fact_count: int,
    is_user: bool,
) -> float:
    base = 0.45 if is_user else 0.35
    if explicit_memory_signal:
        base += 0.32
    if deep_signal or auto_deep_signal:
        base += 0.18
    if structured_fact_count > 0:
        base += 0.12
    return max(0.05, min(1.0, base))


def _event_ttl_from_importance(ts: int, importance: float) -> int | None:
    # Keep important events indefinitely; low-value events age out.
    if importance >= 0.62:
        return None
    days = 30 if importance >= 0.35 else 14
    return int(ts + days * 86400)


def _extract_action_summaries(metadata: Dict[str, Any] | None) -> List[str]:
    if not isinstance(metadata, dict):
        return []
    out: List[str] = []
    raw = metadata.get("actionSummaries")
    if isinstance(raw, list):
        for x in raw:
            s = " ".join(str(x or "").split()).strip()
            if s:
                out.append(s[:240])
    # backward-compatible alias
    raw2 = metadata.get("actions")
    if isinstance(raw2, list):
        for x in raw2:
            s = " ".join(str(x or "").split()).strip()
            if s:
                out.append(s[:240])
    uniq: List[str] = []
    seen = set()
    for s in out:
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return uniq[:12]


def ingest_turn(
    *,
    db: MemqDB,
    session_key: str,
    user_text: str,
    assistant_text: str,
    ts: int,
    dim: int,
    bits_per_dim: int,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, int]:
    _ = dim
    _ = bits_per_dim
    user_text = _sanitize_turn_text(user_text)
    assistant_text = _sanitize_turn_text(assistant_text)

    wrote = {"surface": 0, "deep": 0, "ephemeral": 0, "quarantined": 0, "rules": 0, "style": 0, "events": 0}

    high_risk_user, reason_user, risk_user = _is_high_risk_text(user_text)
    if high_risk_user:
        db.add_quarantine(None, user_text, reason_user, risk_user)
        wrote["quarantined"] += 1
        user_text = ""

    high_risk_assistant, reason_assistant, risk_assistant = _is_high_risk_text(assistant_text)
    if high_risk_assistant:
        db.add_quarantine(None, assistant_text, reason_assistant, risk_assistant)
        wrote["quarantined"] += 1
        assistant_text = ""

    if not user_text.strip() and not assistant_text.strip():
        return wrote

    # Rules and style extraction from user text.
    rules = extract_rule_updates(user_text)
    styles = extract_style_updates(user_text)
    wrote["rules"] += apply_rule_updates(db, rules)
    wrote["style"] += apply_style_updates(db, styles)
    style_update_intent = len(styles) > 0
    explicit_memory_signal = bool(EXPLICIT_REMEMBER_RE.search(user_text))
    explicit_forget_signal = bool(EXPLICIT_FORGET_RE.search(user_text))
    structured_facts = _extract_structured_facts(user_text, styles, rules, explicit_memory_signal)
    fact_keys = _extract_fact_keys(user_text, styles, rules)
    for f in structured_facts:
        fk = str(f.get("fact_key") or "")
        if fk and fk not in fact_keys:
            fact_keys.append(fk)

    pref_events = extract_preference_events(user_text)
    for key, value, weight, explicit, source in pref_events:
        db.add_preference_event(
            key=key,
            value=value,
            weight=weight,
            explicit=explicit,
            source=source,
            evidence_uri=f"session:{session_key}:{ts}",
            created_at=ts,
        )
        wrote["events"] += 1

    policy_profile = db.get_memory_policy_profile()
    retention_default = str((policy_profile.get("retention.default") or {}).get("value") or "").strip().lower()
    policy_ttl_days = _policy_ttl_days(db, default_days=45)

    # Surface memory for every turn (unless text totally empty).
    summary = _surface_summary(user_text, assistant_text)
    if summary:
        db.add_or_merge_memory_item(
            session_key=session_key,
            layer="surface",
            text=summary,
            summary=summary,
            importance=0.55,
            tags={"kind": "turn", "ts": ts, "fact_keys": fact_keys},
            emb_f16=None,
            emb_q=None,
            emb_dim=0,
            source="turn",
        )
        wrote["surface"] += 1

    deep_signal = explicit_memory_signal or bool(structured_facts)
    # Promote structured user facts even without explicit "remember".
    auto_deep_signal = _contains_stable_fact_signal(user_text) and len(user_text.strip()) >= 24
    deep_signal = deep_signal or (auto_deep_signal and not style_update_intent)
    if explicit_forget_signal or retention_default == "surface_only":
        deep_signal = False

    durable_signal = bool(
        EXPLICIT_REMEMBER_RE.search(user_text)
        or re.search(r"(常に|rule|policy|identity|一人称|呼称|口調|性格|style)", user_text, re.IGNORECASE)
    )
    if auto_deep_signal and not style_update_intent:
        durable_signal = True
    if structured_facts:
        durable_signal = True
    if explicit_forget_signal or retention_default == "surface_only":
        durable_signal = False

    if deep_signal:
        structured_written = 0
        for fact in structured_facts:
            fact_key = str(fact.get("fact_key") or "")
            fact_value = str(fact.get("value") or "")
            novelty, same_value_exists = _fact_novelty(db, session_key, fact_key, fact_value)
            if same_value_exists:
                continue
            repetition = _fact_repetition(db, session_key, fact_key)
            subj_match = _subject_match(user_text, fact)
            gate_score, gate_threshold = _write_gate_score(user_text, fact, novelty, repetition, subj_match)
            if gate_score < gate_threshold:
                continue

            fact = dict(fact)
            fact["ts"] = int(ts)
            fact_ttl_days = int(fact.get("ttl_days") or policy_ttl_days)
            if not explicit_memory_signal:
                fact_ttl_days = max(1, min(fact_ttl_days, policy_ttl_days))
            ttl_expires_at = ts + (fact_ttl_days * 86400)
            deep_text = _structured_fact_summary(fact)
            deep_id = db.add_memory_item(
                session_key=session_key,
                layer="deep",
                text=deep_text,
                summary=deep_text,
                importance=0.80,
                tags={
                    "kind": "structured_fact",
                    "ts": ts,
                    "fact_keys": [fact_key],
                    "fact": fact,
                    "gate": {
                        "score": round(gate_score, 3),
                        "threshold": round(gate_threshold, 3),
                        "novelty": round(novelty, 3),
                        "repetition": round(repetition, 3),
                        "subject_match": round(subj_match, 3),
                    },
                },
                emb_f16=None,
                emb_q=None,
                emb_dim=0,
                ttl_expires_at=ttl_expires_at,
                source="turn",
            )
            if fact_key:
                wrote["deep"] += db.expire_conflicting_fact_keys("deep", session_key, [fact_key], deep_id)
            wrote["deep"] += 1
            structured_written += 1

            if durable_signal:
                session_sig = deep_text[:220].strip().lower()
                if not _exists_summary(db, "deep", "global", session_sig):
                    gid = db.add_memory_item(
                        session_key="global",
                        layer="deep",
                        text=deep_text,
                        summary=deep_text,
                        importance=0.88,
                        tags={"kind": "durable_global_fact", "ts": ts, "fact_keys": [fact_key], "fact": fact},
                        emb_f16=None,
                        emb_q=None,
                        emb_dim=0,
                        source="turn",
                    )
                    if fact_key:
                        wrote["deep"] += db.expire_conflicting_fact_keys(
                            "deep",
                            "global",
                            [fact_key],
                            gid,
                        )
                    wrote["deep"] += 1

        # fallback deep: only explicit-remember, and store as structured note fact
        if structured_written == 0 and explicit_memory_signal and not structured_facts:
            deep_text = _deep_candidate(user_text, assistant_text)
            if deep_text:
                note_keys = infer_text_fact_keys(deep_text)
                if not note_keys:
                    note_keys = ["memory.note.generic"]
                primary_note_key = str(note_keys[0])
                note_fact = {
                    "subject": "user",
                    "relation": "memory.note",
                    "value": deep_text[:120],
                    "fact_key": primary_note_key,
                    "confidence": 0.62,
                    "source": "user_msg",
                    "stable": True,
                    "ttl_days": 120,
                    "explicit": True,
                    "ts": int(ts),
                }
                deep_text = _structured_fact_summary(note_fact)
                session_sig = deep_text[:220].strip().lower()
                note_ttl_days = int(note_fact.get("ttl_days") or policy_ttl_days)
                if not explicit_memory_signal:
                    note_ttl_days = max(1, min(note_ttl_days, policy_ttl_days))
                note_ttl = ts + (note_ttl_days * 86400)
                deep_id = db.add_memory_item(
                    session_key=session_key,
                    layer="deep",
                    text=deep_text,
                    summary=deep_text[:220],
                    importance=0.70,
                    tags={
                        "kind": "structured_fact",
                        "ts": ts,
                        "fact_keys": sorted(list(set(["memory.note", *note_keys]))),
                        "fact": note_fact,
                    },
                    emb_f16=None,
                    emb_q=None,
                    emb_dim=0,
                    ttl_expires_at=note_ttl,
                    source="turn",
                )
                if primary_note_key and not primary_note_key.startswith("memory.note"):
                    wrote["deep"] += db.expire_conflicting_fact_keys("deep", session_key, [primary_note_key], deep_id)
                wrote["deep"] += 1

                # Mirror selective deep facts to global so memory survives session-key churn.
                if durable_signal and not _exists_summary(db, "deep", "global", session_sig):
                    gid = db.add_memory_item(
                        session_key="global",
                        layer="deep",
                        text=deep_text,
                        summary=deep_text[:220],
                        importance=0.78,
                        tags={
                            "kind": "durable_global_fact",
                            "ts": ts,
                            "fact_keys": sorted(list(set(["memory.note", *note_keys]))),
                            "fact": note_fact,
                        },
                        emb_f16=None,
                        emb_q=None,
                        emb_dim=0,
                        source="turn",
                    )
                    if primary_note_key and not primary_note_key.startswith("memory.note"):
                        wrote["deep"] += db.expire_conflicting_fact_keys(
                            "deep",
                            "global",
                            [primary_note_key],
                            gid,
                        )
                    wrote["deep"] += 1

    # Timeline/episodic events for time-scoped recall ("yesterday", "recently", etc).
    structured_count = len(structured_facts)
    user_imp = _event_importance(
        explicit_memory_signal=explicit_memory_signal,
        deep_signal=deep_signal,
        auto_deep_signal=auto_deep_signal,
        structured_fact_count=structured_count,
        is_user=True,
    )
    asst_imp = _event_importance(
        explicit_memory_signal=explicit_memory_signal,
        deep_signal=deep_signal,
        auto_deep_signal=auto_deep_signal,
        structured_fact_count=structured_count,
        is_user=False,
    )
    if user_text.strip():
        db.add_event(
            session_key=session_key,
            ts=ts,
            actor="user",
            kind="chat",
            summary=user_text[:320],
            tags={"source": "ingest_turn", "role": "user"},
            importance=user_imp,
            ttl_expires_at=_event_ttl_from_importance(ts, user_imp),
        )
        wrote["events"] += 1
    if assistant_text.strip():
        db.add_event(
            session_key=session_key,
            ts=ts,
            actor="assistant",
            kind="chat",
            summary=assistant_text[:320],
            tags={"source": "ingest_turn", "role": "assistant"},
            importance=asst_imp,
            ttl_expires_at=_event_ttl_from_importance(ts, asst_imp),
        )
        wrote["events"] += 1
    for a in _extract_action_summaries(metadata):
        action_risky, action_reason, action_risk = _is_high_risk_text(a)
        if action_risky:
            db.add_quarantine(None, a, action_reason, action_risk)
            wrote["quarantined"] += 1
            continue
        db.add_event(
            session_key=session_key,
            ts=ts,
            actor="assistant",
            kind="action",
            summary=a,
            tags={"source": "agent_end_meta", "role": "assistant"},
            importance=0.68,
            ttl_expires_at=ts + 45 * 86400,
        )
        wrote["events"] += 1

    # Ephemeral for short low-value chatter.
    if len(user_text.strip()) <= 64 and not deep_signal:
        eph = _surface_summary(user_text, assistant_text)
        if eph:
            db.add_memory_item(
                session_key=session_key,
                layer="ephemeral",
                text=eph,
                summary=eph[:120],
                importance=0.3,
                tags={"kind": "ephemeral", "ts": ts},
                emb_f16=None,
                emb_q=None,
                emb_dim=0,
                # Ephemeral expires by low-value decay/prune, not fixed wall-clock TTL.
                ttl_expires_at=None,
                source="turn",
            )
            wrote["ephemeral"] += 1

    # Keep surface bounded.
    db.trim_layer_size("surface", session_key, max_items=240)

    return wrote
