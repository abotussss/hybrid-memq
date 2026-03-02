from __future__ import annotations

import math
import re
from typing import Any, Dict, List

from .db import MemqDB

NOISE_SUMMARY_RE = re.compile(
    r"(<MEM(?:RULES|STYLE|CTX)\s+v1>|\[MEM(?:RULES|STYLE|CTX)\s+v1\]|\[\[reply_to_current\]\]|read\s+(?:agents|soul|identity|heartbeat)\.md|workspace context)",
    re.IGNORECASE,
)


def _is_noise_summary(text: str) -> bool:
    s = text or ""
    if not s:
        return False
    m = NOISE_SUMMARY_RE.search(s)
    # Guard against accidental empty-match regexes.
    return bool(m and m.group(0))


def _score(lex: float, importance: float, usage_count: int, age_sec: int) -> float:
    recency = math.exp(-max(0, age_sec) / 172800.0)
    freq = math.log1p(max(0, usage_count))
    return 0.95 * lex + 0.45 * recency + 0.15 * freq + 0.5 * float(importance)


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


def search_surface(db: MemqDB, session_key: str, query_text: str, top_k: int) -> List[Dict[str, Any]]:
    rows = db.list_memory_items("surface", session_key, limit=2000)
    q_tokens = _tokenize(query_text)
    now = __import__("time").time()
    scored: List[Dict[str, Any]] = []
    for r in rows:
        summary_raw = str(r["summary"] or "")
        if _is_noise_summary(summary_raw):
            continue
        lex = _lex_overlap(q_tokens, summary_raw)
        if lex <= 0.0 and not q_tokens:
            lex = 0.01
        age = int(now - int(r["last_access_at"]))
        s = _score(lex, float(r["importance"]), int(r["usage_count"]), age)
        scored.append(
            {
                "id": str(r["id"]),
                "score": float(s),
                "sim": float(lex),
                "lex": float(lex),
                "summary": summary_raw,
                "layer": "surface",
                "importance": float(r["importance"]),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(1, top_k)]
