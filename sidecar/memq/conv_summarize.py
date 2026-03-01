from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from .models import Message
from .text_sanitize import strip_memq_blocks


IMPORTANT_PATTERNS = [
    re.compile(r"\b(remember|must|always|never|rule|constraint|policy|deadline|todo|task|goal)\b", re.IGNORECASE),
    re.compile(r"(覚えて|必ず|禁止|ルール|制約|期限|TODO|課題|目標|方針)"),
]
PROMOTE_PATTERNS = [
    re.compile(r"\b(remember|must|always|never|rule|constraint|policy|deadline|todo|task|goal|family|persona|identity|call me)\b", re.IGNORECASE),
    re.compile(r"(覚えて|必ず|禁止|ルール|制約|期限|TODO|課題|目標|方針|家族|妻|夫|子ども|犬|猫|人格|口調|呼称|一人称|検索)"),
]
PROMOTE_EXCLUDE_PATTERNS = [
    re.compile(r"(長期記憶.*(?:不足|不完全|課題|弱い)|参照できる記憶コンテキスト.*不足|取り扱い不足)", re.IGNORECASE),
    re.compile(r"(OpenClawで動く.*アシスタント|僕は.*アシスタント)", re.IGNORECASE),
    re.compile(r"((?:お前|あなた|you).*(?:として振る舞|になりき|act as|roleplay)|(?:しろ|しなさい|してください)\s*$)", re.IGNORECASE),
]

UNTRUSTED_META_RE = re.compile(r"Conversation info \(untrusted metadata\):[\s\S]*?(?:```[\s\S]*?```)?", re.IGNORECASE)
FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.IGNORECASE)
RUNTIME_META_RE = re.compile(
    r"(read\s+(?:agents|soul|identity|heartbeat)\.md|workspace context|follow it strictly|do not infer or repeat old tasks)",
    re.IGNORECASE,
)


def _normalize_line(s: str) -> str:
    t = s or ""
    t = strip_memq_blocks(t)
    t = UNTRUSTED_META_RE.sub(" ", t)
    t = FENCED_BLOCK_RE.sub(" ", t)
    t = re.sub(r"\[\[reply_to_current\]\]", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\*{1,3}", "", t)
    t = re.sub(r"`+", "", t)
    if RUNTIME_META_RE.search(t):
        return ""
    t = re.sub(r"\bsha256:[0-9a-f]{16,}\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"backup=/\S+", " ", t)
    t = re.sub(r"/Users/\S+", " ", t)
    t = " ".join(t.strip().split())
    if t.lower().startswith("x:"):
        return ""
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


def deep_summary_candidates(summary_text: str, max_lines: int = 8) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in (summary_text or "").split("\n"):
        t = _normalize_line(raw)
        if not t:
            continue
        if t.startswith("u:") or t.startswith("a:") or t.startswith("x:"):
            t = t[2:].strip()
        if len(t) < 10:
            continue
        if any(p.search(t) for p in PROMOTE_EXCLUDE_PATTERNS):
            continue
        if not any(p.search(t) for p in PROMOTE_PATTERNS):
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t[:220])
        if len(out) >= max_lines:
            break
    return out
