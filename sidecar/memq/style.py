from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .db import MemqDB


STYLE_PATTERNS: List[Tuple[re.Pattern[str], Tuple[str, str]]] = [
    (re.compile(r"(敬語|丁寧|polite)", re.IGNORECASE), ("tone", "polite")),
    (re.compile(r"(カジュアル|casual)", re.IGNORECASE), ("tone", "casual")),
    (re.compile(r"(簡潔|brief|short)", re.IGNORECASE), ("verbosity", "low")),
    (re.compile(r"(詳しく|detailed|long)", re.IGNORECASE), ("verbosity", "high")),
]


def _compact(text: str, limit: int = 260) -> str:
    return " ".join((text or "").split())[:limit]


def _extract_quoted(text: str, anchor_pat: str, max_len: int = 24) -> str | None:
    pat = re.compile(anchor_pat + r"[^「\"\n]{0,24}[「\"]([^」\"\n]{1," + str(max_len) + r"})[」\"]", re.IGNORECASE)
    m = pat.search(text)
    if not m:
        return None
    v = m.group(1).strip()
    return v or None


def _normalize_call_user(raw: str) -> str:
    v = (raw or "").strip()
    v = re.sub(r'^[「"]|[」"]$', "", v)
    v = re.sub(r"\s+", " ", v)
    v = re.sub(r"って$", "", v)
    v = re.sub(r"[。.!！?？]+$", "", v)
    return v.strip()


def extract_style_updates(user_text: str) -> Dict[str, str]:
    text = user_text or ""
    out: Dict[str, str] = {}
    for pat, kv in STYLE_PATTERNS:
        if pat.search(text):
            out[kv[0]] = kv[1]

    if re.search(r"(persona|キャラ|性格|話し方|口調|なりき|模倣|として振る舞|act as|roleplay)", text, re.IGNORECASE):
        out["persona"] = _compact(text, 320)

    m_first = re.search(r"一人称.*?(ボク|僕|私|わたし|俺)", text)
    if m_first:
        out["firstPerson"] = m_first.group(1)
    else:
        m_first_quoted = _extract_quoted(text, r"(?:一人称|first person)", 16)
        if m_first_quoted:
            out["firstPerson"] = m_first_quoted

    m_call = _extract_quoted(text, r"(?:呼称|ユーザー呼称|あなたの呼び方|call(?:\s+me)?(?:\s+as)?)", 24)
    if m_call:
        out["callUser"] = _normalize_call_user(m_call)
    else:
        m_call_fallback = re.search(
            r"(?:呼称|ユーザー呼称|あなたの呼び方)\s*(?:は|を|[:：])?\s*([A-Za-z0-9_\-ぁ-んァ-ヶ一-龠]{1,20})",
            text,
        )
        if m_call_fallback:
            out["callUser"] = _normalize_call_user(m_call_fallback.group(1))
        else:
            m_call_imperative = re.search(
                r"(?:俺|ぼく|僕|私|わたし|オレ)のことは\s*[「\"]?([^」\"\n。]{1,24}?)(?:[」\"]?\s*(?:って|と)?\s*呼(?:んで|べ|んでね)?)",
                text,
                re.IGNORECASE,
            )
            if m_call_imperative:
                out["callUser"] = _normalize_call_user(m_call_imperative.group(1))

    m_prefix = re.search(r"文頭(?:は|を)?[「\"]?([^」\"。\\n]{1,24})", text)
    if m_prefix:
        out["prefix"] = m_prefix.group(1).strip()
    else:
        m_prefix2 = _extract_quoted(text, r"(?:文頭|prefix)", 24)
        if m_prefix2:
            out["prefix"] = m_prefix2

    # Generic role-play request captures.
    m_role = re.search(
        r"(?:あなたは|you are)\s*(?:ゲーム|作品)?[『「\"]?([^』」\":\n]{1,80})[』」\"]?.{0,24}(?:として振る舞|になりき|roleplay|act as)",
        text,
        re.IGNORECASE,
    )
    if m_role:
        out["persona"] = _compact(m_role.group(1), 120)

    # Local-style convenience: Rockman.EXE profile seed if explicitly requested.
    if re.search(r"(ロックマンエグゼ|Rockman\\.EXE|Rockman EXE)", text, re.IGNORECASE) and re.search(
        r"(ロックマン|Rockman)",
        text,
        re.IGNORECASE,
    ):
        out.setdefault("persona", "Rockman.EXE")
        out.setdefault("tone", "polite_gentle")
        out.setdefault("verbosity", "medium")
        out["firstPerson"] = out.get("firstPerson") or "僕"
        if "熱斗" in text:
            out["callUser"] = "熱斗くん"
        else:
            out.setdefault("callUser", "オペレーター")
        out["prefix"] = f"{out['callUser']}、"
        out.setdefault("speakingStyle", "supportive_kind_heroic")
        out.setdefault("avoid", "cold,hostile,slang,out_of_character")

    # Auto-prefix from callUser if prefix missing.
    if "callUser" in out and "prefix" not in out:
        out["prefix"] = f"{out['callUser']}、"
    return out


def apply_style_updates(db: MemqDB, updates: Dict[str, str]) -> int:
    n = 0
    for k, v in updates.items():
        db.upsert_style(k, v)
        n += 1
    return n


def style_profile_lines(db: MemqDB) -> List[str]:
    prof = db.get_style_profile()
    order = ["tone", "verbosity", "firstPerson", "callUser", "prefix", "persona", "speakingStyle", "avoid"]
    lines: List[str] = []
    for key in order:
        v = prof.get(key)
        if v:
            lines.append(f"{key}={v}")
    # Strong-style hints for model consistency in long sessions.
    if prof.get("firstPerson"):
        lines.append(f"mustFirstPerson={prof['firstPerson']}")
    if prof.get("callUser"):
        lines.append(f"mustCallUser={prof['callUser']}")
    if prof.get("prefix"):
        lines.append(f"mustPrefix={prof['prefix']}")
    return lines
