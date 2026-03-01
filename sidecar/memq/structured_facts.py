from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


_NAME_LIKE_RE = re.compile(r"^[A-Za-z0-9ぁ-んァ-ヶ一-龠ー._\\-]{1,24}$")
_PREFIX_RE = re.compile(r"^(?:u|a|x):\s*", re.IGNORECASE)


@dataclass(frozen=True)
class StructuredPattern:
    pattern: re.Pattern[str]
    subject: str
    relation: str
    fact_key: str
    confidence: float


# Centralized extraction patterns to avoid endpoint-local special cases.
PATTERNS: Sequence[StructuredPattern] = (
    StructuredPattern(
        pattern=re.compile(r"(?:妻|奥さま|奥さん|夫|旦那|husband|wife)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="user",
        relation="family.spouse",
        fact_key="profile.family.spouse",
        confidence=0.75,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:愛犬|犬|猫|ペット|dog|cat|pet)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="user",
        relation="family.pet",
        fact_key="profile.family.pet",
        confidence=0.75,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:子ども|子供|息子|娘|child|son|daughter)\s*(?:は|が|:|：)\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="user",
        relation="family.child",
        fact_key="profile.family.child",
        confidence=0.72,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:呼称|呼び方|ユーザー呼称)\s*(?:は|が|:|：)?\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="assistant",
        relation="identity.call_user",
        fact_key="profile.identity.call_user",
        confidence=0.76,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:一人称)\s*(?:は|が|:|：)?\s*([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="assistant",
        relation="identity.first_person",
        fact_key="profile.identity.first_person",
        confidence=0.74,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:persona=|人格[:：]?\s*)([A-Za-z0-9ぁ-んァ-ヶ一-龠ー\\-]{1,24})", re.IGNORECASE),
        subject="assistant",
        relation="persona.role",
        fact_key="profile.persona.role",
        confidence=0.78,
    ),
    StructuredPattern(
        pattern=re.compile(r"(?:検索|search).*(brave|google|bing|duckduckgo)", re.IGNORECASE),
        subject="user",
        relation="preference.search_engine",
        fact_key="pref.search.engine",
        confidence=0.72,
    ),
)


def normalize_fact_value(value: str, max_len: int = 48) -> str:
    return " ".join((value or "").split()).strip()[:max_len]


def plausible_fact_value(fact_key: str, value: str) -> bool:
    v = normalize_fact_value(value)
    if not v:
        return False
    if fact_key in {"profile.identity.first_person"}:
        return len(v) <= 12 and not any(ch.isspace() for ch in v)
    if fact_key in {
        "profile.family.spouse",
        "profile.family.pet",
        "profile.family.child",
        "profile.identity.call_user",
    }:
        return bool(_NAME_LIKE_RE.match(v))
    return True


def is_durable_fact_text(text: str) -> bool:
    t = text or ""
    return bool(
        re.search(r"(remember|always|must|rule|policy|constraint|goal|preference|identity)", t, re.IGNORECASE)
        or re.search(r"(覚えて|必ず|ルール|方針|制約|目標|好み|口調|呼称|一人称|性格)", t)
    )


def dedupe_facts(facts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for f in facts:
        sig = f"{f.get('fact_key') or ''}::{f.get('value') or ''}".lower()
        if not sig or sig in seen:
            continue
        seen.add(sig)
        out.append(dict(f))
    return out


def extract_structured_facts_from_text(
    text: str,
    *,
    ts: int,
    source: str,
    confidence_scale: float = 1.0,
    strip_prefix: bool = False,
    ttl_days: int = 365,
) -> List[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return []
    if strip_prefix:
        t = _PREFIX_RE.sub("", t)

    out: List[Dict[str, Any]] = []

    for p in PATTERNS:
        m = p.pattern.search(t)
        if not m:
            continue
        value = normalize_fact_value(m.group(1))
        if not plausible_fact_value(p.fact_key, value):
            continue
        out.append(
            {
                "subject": p.subject,
                "relation": p.relation,
                "value": value,
                "fact_key": p.fact_key,
                "confidence": float(max(0.01, min(1.0, p.confidence * confidence_scale))),
                "source": source,
                "stable": True,
                "ttl_days": int(ttl_days),
                "explicit": False,
                "ts": int(ts),
            }
        )

    if re.search(r"(結論から|summary first|first then details)", t, re.IGNORECASE):
        out.append(
            {
                "subject": "assistant",
                "relation": "rule.output_order",
                "value": "summary_first",
                "fact_key": "rule.output.order",
                "confidence": float(max(0.01, min(1.0, 0.70 * confidence_scale))),
                "source": source,
                "stable": True,
                "ttl_days": int(ttl_days),
                "explicit": False,
                "ts": int(ts),
            }
        )

    if re.search(r"(箇条書き|bullet|list format)", t, re.IGNORECASE):
        out.append(
            {
                "subject": "assistant",
                "relation": "rule.output_format",
                "value": "bullets",
                "fact_key": "rule.output.format",
                "confidence": float(max(0.01, min(1.0, 0.68 * confidence_scale))),
                "source": source,
                "stable": True,
                "ttl_days": int(ttl_days),
                "explicit": False,
                "ts": int(ts),
            }
        )

    return dedupe_facts(out)


def structured_fact_summary(f: Dict[str, Any], max_len: int = 220) -> str:
    rel = str(f.get("relation") or "")
    val = str(f.get("value") or "")
    subj = str(f.get("subject") or "user")
    conf = float(f.get("confidence", 0.7))
    src = str(f.get("source") or "unknown")
    ttl_days = int(f.get("ttl_days") or 365)

    if rel == "family.spouse":
        core = f"家族: 妻={val}"
    elif rel == "family.pet":
        core = f"家族: ペット={val}"
    elif rel == "family.child":
        core = f"家族: 子ども={val}"
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

    return f"{core} | subject={subj} | conf={conf:.2f} | src={src} | ttl={ttl_days}d"[:max_len]


def parse_fact_signature_from_row(row: Dict[str, Any]) -> Tuple[str, str]:
    try:
        tags = json.loads(str(row.get("tags") or "{}"))
    except Exception:
        return "", ""
    fact = tags.get("fact") if isinstance(tags, dict) else {}
    if not isinstance(fact, dict):
        return "", ""
    fk = str(fact.get("fact_key") or "")
    fv = normalize_fact_value(str(fact.get("value") or "")).lower()
    return fk, fv
