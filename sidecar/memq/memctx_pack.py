from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple
import re

from .db import MemqDB
from .rules import extract_allowed_languages_from_rules
from .style import style_profile_lines


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
    rules = db.list_rules()
    for row in rules:
        body = str(row["body"])
        lines.append(body)

    # derive language allowlist if rule missing
    has_lang = any(x.startswith("language.allowed=") for x in lines)
    if not has_lang:
        langs = extract_allowed_languages_from_rules(db)
        lines.append(f"language.allowed={','.join(langs)}")

    lines = fit_budget(lines, budget_tokens)
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

    def _allow_ctx_line(s: str) -> bool:
        t = (s or "").strip()
        if not t:
            return False
        if rule_like.search(t):
            return False
        if style_like.search(t):
            return False
        return True

    lines: List[str] = [f"budget_tokens={budget_tokens}", f"q={prompt[:120].replace(chr(10), ' ')}"]

    convsurf = db.get_conv_summary(session_key, "surface_only")
    if convsurf:
        lines.append(f"convsurf={convsurf[:360].replace(chr(10), ' | ')}")

    convdeep = db.get_conv_summary(session_key, "deep")
    if convdeep:
        lines.append(f"convdeep={convdeep[:360].replace(chr(10), ' | ')}")

    for idx, item in enumerate(surface[:8]):
        summary = str(item.get("summary", ""))[:180]
        if _allow_ctx_line(summary):
            lines.append(f"s{idx+1}={summary}")

    for idx, item in enumerate(deep[:8]):
        summary = str(item.get("summary", ""))[:180]
        if _allow_ctx_line(summary):
            lines.append(f"d{idx+1}={summary}")

    # Mark ephemeral state compactly.
    eph = db.list_memory_items("ephemeral", session_key, limit=3)
    for idx, row in enumerate(eph):
        summary = str(row["summary"])[:120].replace("\n", " ")
        lines.append(f"e{idx+1}={summary}")

    lines = fit_budget(lines, budget_tokens)
    return "\n".join(lines)
