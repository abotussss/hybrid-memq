from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from .models import Message


IMPORTANT_PATTERNS = [
    re.compile(r"\b(remember|must|always|never|rule|constraint|policy|deadline|todo|task|goal)\b", re.IGNORECASE),
    re.compile(r"(覚えて|必ず|禁止|ルール|制約|期限|TODO|課題|目標|方針)"),
]


def _normalize_line(s: str) -> str:
    t = " ".join(s.strip().split())
    return t


def _pick_lines(messages: Sequence[Message], max_lines: int, prefer_user: bool = True) -> List[str]:
    lines: List[str] = []
    for m in reversed(messages):
        txt = _normalize_line(m.text)
        if not txt:
            continue
        if prefer_user and m.role != "user" and len(lines) < max_lines // 2:
            continue
        score = 0
        for pat in IMPORTANT_PATTERNS:
            if pat.search(txt):
                score += 1
        prefix = "u" if m.role == "user" else ("a" if m.role == "assistant" else "x")
        packed = f"{prefix}:{txt[:220]}"
        if score > 0:
            lines.insert(0, packed)
        else:
            lines.append(packed)
        if len(lines) >= max_lines:
            break
    # stable unique preserving order
    seen = set()
    out: List[str] = []
    for ln in lines:
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ln)
    return out[:max_lines]


def merge_summary(old_summary: str, new_block: str, max_chars: int) -> str:
    buf: List[str] = []
    for ln in (old_summary or "").split("\n") + (new_block or "").split("\n"):
        t = _normalize_line(ln)
        if t:
            buf.append(t)
    # de-dup by normalized text
    seen = set()
    merged: List[str] = []
    for ln in buf:
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(ln)
    joined = "\n".join(merged)
    if len(joined) <= max_chars:
        return joined
    # keep tail because latest context is higher utility
    return joined[-max_chars:]


def summarize_for_surface(messages: Sequence[Message]) -> str:
    lines = _pick_lines(messages, max_lines=8, prefer_user=True)
    return "\n".join(lines)


def summarize_for_deep(messages: Sequence[Message]) -> str:
    lines = _pick_lines(messages, max_lines=16, prefer_user=False)
    return "\n".join(lines)
