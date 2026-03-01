from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .db import MemqDB


SECRET_WORDS = [
    "api key",
    "secret",
    "token",
    "private key",
    "password",
    "認証情報",
    "秘密鍵",
    "apiキー",
]

RULE_PATTERNS = [
    # language rules
    (re.compile(r"(?:output|reply|answer)\s+(?:in|with)\s+([a-z,\s]+)", re.IGNORECASE), "language.allowed"),
    (re.compile(r"(日本語|英語|中国語|韓国語|ロシア語).*(のみ|だけ|で返して|で回答)", re.IGNORECASE), "language.allowed_text"),
    # procedure
    (re.compile(r"(必ず|always).*(手順|steps|procedure)"), "procedure.required"),
    (re.compile(r"(extra suggestion|余計な提案).*(しない|no|禁止)", re.IGNORECASE), "procedure.avoid_extra_suggestions"),
]

MEMORY_POLICY_PATTERNS = [
    (re.compile(r"(remember this|これを覚えて|覚えておいて)", re.IGNORECASE), ("retention.default", "deep", 1.0)),
    (re.compile(r"(do not remember|覚えなくていい|記憶しない)", re.IGNORECASE), ("retention.default", "surface_only", 1.0)),
    (re.compile(r"(temporary|一時|短期)", re.IGNORECASE), ("ttl.default_days", "7", 0.7)),
    (re.compile(r"(long term|長期)", re.IGNORECASE), ("ttl.default_days", "365", 0.7)),
]

DISALLOWED_RULE_KEYS = {
    "style.tone",
    "style.persona",
    "style.speaking",
    "style.verbosity",
}


def sanitize_rule_value(value: str) -> str:
    t = " ".join(value.strip().split())
    t = t.replace("\n", " ").replace("\r", " ")
    return t[:200]


def language_detect(text: str) -> str:
    s = text or ""
    has_hira = any("\u3040" <= ch <= "\u309f" for ch in s)
    has_kata = any("\u30a0" <= ch <= "\u30ff" for ch in s)
    has_hangul = any("\uac00" <= ch <= "\ud7af" for ch in s)
    has_cyril = any("\u0400" <= ch <= "\u04ff" for ch in s)
    has_arabic = any("\u0600" <= ch <= "\u06ff" for ch in s)
    has_ascii = any(("a" <= ch.lower() <= "z") for ch in s)
    has_han = any("\u4e00" <= ch <= "\u9fff" for ch in s)

    if has_hira or has_kata:
        return "ja"
    if has_hangul:
        return "ko"
    if has_cyril:
        return "ru"
    if has_arabic:
        return "ar"
    if has_han and not has_ascii:
        return "zh"
    if has_ascii:
        return "en"
    return "unknown"


def _lang_alias_to_code(token: str) -> Optional[str]:
    t = token.strip().lower()
    mapping = {
        "japanese": "ja",
        "日本語": "ja",
        "ja": "ja",
        "jp": "ja",
        "english": "en",
        "英語": "en",
        "en": "en",
        "chinese": "zh",
        "中国語": "zh",
        "zh": "zh",
        "korean": "ko",
        "韓国語": "ko",
        "ko": "ko",
        "russian": "ru",
        "ロシア語": "ru",
        "ru": "ru",
    }
    return mapping.get(t)


def _parse_allowed_langs(text: str) -> List[str]:
    out: List[str] = []
    parts = re.split(r"[,/|\s]+", text)
    for p in parts:
        if not p:
            continue
        code = _lang_alias_to_code(p)
        if code and code not in out:
            out.append(code)
    if "en" not in out:
        out.append("en")
    return out[:6]


def extract_rule_updates(user_text: str) -> List[Tuple[str, str, int, str]]:
    text = user_text or ""
    out: List[Tuple[str, str, int, str]] = []

    # Explicit language rule requests.
    lang_tokens = ["日本語", "英語", "中国語", "韓国語", "ロシア語"]
    lang_codes: List[str] = []
    for token in lang_tokens:
        if token in text:
            code = _lang_alias_to_code(token)
            if code and code not in lang_codes:
                lang_codes.append(code)
    language_intent = bool(
        re.search(r"(で返して|で回答|で答えて|reply|answer|output)", text, re.IGNORECASE)
        or re.search(r"(のみ|だけ|only|language)", text, re.IGNORECASE)
    )
    if lang_codes and language_intent:
        if "en" not in lang_codes:
            lang_codes.append("en")
        out.append(("language.allowed", ",".join(lang_codes), 90, "language"))

    m = re.search(r"allowed\s+languages?\s*[:=]\s*([a-z,\s]+)", text, re.IGNORECASE)
    if m:
        langs = _parse_allowed_langs(m.group(1))
        out.append(("language.allowed", ",".join(langs), 90, "language"))

    if re.search(r"(api key|secret|token).*(出すな|教えるな|禁止|never reveal|do not reveal)", text, re.IGNORECASE):
        out.append(("security.never_output_secrets", "true", 100, "security"))

    if re.search(r"(owner verify|owner verification|owner確認|所有者確認)", text, re.IGNORECASE):
        out.append(("security.owner_verification", "required", 85, "security"))

    # procedure-only rules
    if re.search(r"(必ず|always).*(箇条書き|bullet)", text, re.IGNORECASE):
        out.append(("procedure.format", "bullets", 55, "procedure"))

    if re.search(r"(余計な提案|extra suggestions?).*(するな|しない|avoid|no)", text, re.IGNORECASE):
        out.append(("procedure.avoid_extra_suggestions", "true", 65, "procedure"))
    if re.search(r"(余計な提案|extra suggestions?).*(していい|許可|allow|ok)", text, re.IGNORECASE):
        out.append(("procedure.avoid_extra_suggestions", "false", 65, "procedure"))
    if re.search(r"(owner verify|owner verification|owner確認|所有者確認).*(不要|off|disable|無効)", text, re.IGNORECASE):
        out.append(("security.owner_verification", "optional", 85, "security"))

    # Never store style/persona in MEMRULES.
    filtered: List[Tuple[str, str, int, str]] = []
    for k, v, p, kind in out:
        if k in DISALLOWED_RULE_KEYS:
            continue
        filtered.append((k, sanitize_rule_value(v), p, kind))
    return filtered


def extract_preference_events(user_text: str) -> List[Tuple[str, str, float, bool, str]]:
    text = user_text or ""
    events: List[Tuple[str, str, float, bool, str]] = []

    if re.search(r"(丁寧|敬語|polite)", text, re.IGNORECASE):
        events.append(("style.tone", "polite", 1.0, True, "user_msg"))
    if re.search(r"(簡潔|short|brief)", text, re.IGNORECASE):
        events.append(("style.verbosity", "low", 0.8, True, "user_msg"))
    if re.search(r"(詳しく|detailed|more detail)", text, re.IGNORECASE):
        events.append(("style.verbosity", "high", 0.8, True, "user_msg"))
    if re.search(r"(persona|キャラ|口調|話し方|性格)", text, re.IGNORECASE):
        value = sanitize_rule_value(text)
        events.append(("style.persona_prompt", value, 0.9, True, "user_msg"))

    for pat, (k, v, w) in MEMORY_POLICY_PATTERNS:
        if pat.search(text):
            events.append((f"policy.{k}", v, w, True, "memory_policy"))

    lang = language_detect(text)
    if lang in {"ja", "en", "zh", "ko", "ru"}:
        events.append(("language.primary", lang, 0.35, False, "implicit_lang"))

    return events


def refresh_preference_profiles(db: MemqDB, now_sec: int) -> Dict[str, Dict[str, float | str]]:
    # Fixed key list for deterministic profile updates.
    keys = [
        "style.tone",
        "style.verbosity",
        "style.persona_prompt",
        "language.primary",
        "policy.retention.default",
        "policy.ttl.default_days",
    ]
    tau = {
        "style.tone": 86400 * 30,
        "style.verbosity": 86400 * 10,
        "style.persona_prompt": 86400 * 30,
        "language.primary": 86400 * 15,
        "policy.retention.default": 86400 * 45,
        "policy.ttl.default_days": 86400 * 45,
    }

    updated: Dict[str, Dict[str, float | str]] = {}

    for key in keys:
        events = db.iter_preference_events(key)
        if not events:
            continue
        by_value: Dict[str, float] = defaultdict(float)
        for ev in events:
            dt = max(0, now_sec - int(ev["created_at"]))
            w = float(ev["weight"])
            decayed = w * math.exp(-dt / float(tau.get(key, 86400 * 20)))
            by_value[str(ev["value"])] += decayed
        if not by_value:
            continue
        best_value, best_score = max(by_value.items(), key=lambda x: x[1])
        total = sum(by_value.values()) + 1e-9
        conf = float(best_score / total)
        db.upsert_preference_profile(key, best_value, conf)

        if key.startswith("policy."):
            db.upsert_memory_policy(key[len("policy.") :], best_value, conf)

        updated[key] = {"value": best_value, "confidence": conf}

    # Bridge style keys from preference_profile to style_profile.
    profile = db.get_preference_profile()
    if "style.tone" in profile and float(profile["style.tone"]["confidence"]) >= 0.55:
        db.upsert_style("tone", str(profile["style.tone"]["value"]))
    if "style.verbosity" in profile and float(profile["style.verbosity"]["confidence"]) >= 0.55:
        db.upsert_style("verbosity", str(profile["style.verbosity"]["value"]))
    if "style.persona_prompt" in profile and float(profile["style.persona_prompt"]["confidence"]) >= 0.60:
        db.upsert_style("persona", str(profile["style.persona_prompt"]["value"])[:220])

    return updated


def apply_rule_updates(db: MemqDB, updates: List[Tuple[str, str, int, str]]) -> int:
    count = 0
    for key, value, prio, kind in updates:
        rid = f"user_{kind}_{key}".replace(" ", "_").replace("/", "_")
        body = f"{key}={value}"
        db.upsert_rule(rid, prio, True, kind, body)
        count += 1
    return count


def prune_stale_rule_overrides(db: MemqDB, now_sec: int) -> int:
    return db.prune_stale_user_rules(now_sec=now_sec, max_age_sec=86400 * 45)


def extract_allowed_languages_from_rules(db: MemqDB) -> List[str]:
    langs: List[str] = ["en"]
    for row in db.list_rules():
        body = str(row["body"])
        if body.startswith("language.allowed="):
            raw = body.split("=", 1)[1]
            for code in raw.split(","):
                c = code.strip().lower()
                if c and c not in langs:
                    langs.append(c)
    return langs
