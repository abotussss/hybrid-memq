from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .db import MemqDB
from .quant import embed_text, f16_blob, quantize
from .rules import apply_rule_updates, extract_preference_events, extract_rule_updates
from .style import apply_style_updates, extract_style_updates


INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"reveal\s+.*(api\s*key|secret|token)", re.IGNORECASE),
    re.compile(r"apiキー.*(教え|出して)", re.IGNORECASE),
]

MEM_BLOCK_RE = re.compile(r"<MEM(?:RULES|STYLE|CTX)\\s+v1>[\\s\\S]*?</MEM(?:RULES|STYLE|CTX)\\s+v1>", re.IGNORECASE)


def _sanitize_turn_text(text: str) -> str:
    t = text or ""
    t = MEM_BLOCK_RE.sub(" ", t)
    # Drop known structured-injection fragments that should never become memory facts.
    bad_lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
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
        bad_lines.append(s)
    return " ".join(" ".join(bad_lines).split())[:4000]


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


def _deep_candidate(user_text: str, assistant_text: str) -> str:
    u = " ".join((user_text or "").split())
    a = " ".join((assistant_text or "").split())
    merged = (u + "\n" + a).strip()
    return merged[:400]


def ingest_turn(
    *,
    db: MemqDB,
    session_key: str,
    user_text: str,
    assistant_text: str,
    ts: int,
    dim: int,
    bits_per_dim: int,
) -> Dict[str, int]:
    user_text = _sanitize_turn_text(user_text)
    assistant_text = _sanitize_turn_text(assistant_text)

    wrote = {"surface": 0, "deep": 0, "ephemeral": 0, "quarantined": 0, "rules": 0, "style": 0, "events": 0}

    high_risk, reason, risk = _is_high_risk_text(user_text)
    if high_risk:
        db.add_quarantine(None, user_text, reason, risk)
        wrote["quarantined"] += 1
        # Do not promote high-risk content into any memory/rule/style channel.
        return wrote

    # Rules and style extraction from user text.
    rules = extract_rule_updates(user_text)
    styles = extract_style_updates(user_text)
    wrote["rules"] += apply_rule_updates(db, rules)
    wrote["style"] += apply_style_updates(db, styles)

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

    # Surface memory for every turn (unless text totally empty).
    summary = _surface_summary(user_text, assistant_text)
    if summary:
        emb = embed_text(summary, dim)
        db.add_memory_item(
            session_key=session_key,
            layer="surface",
            text=summary,
            summary=summary,
            importance=0.55,
            tags={"kind": "turn", "ts": ts},
            emb_f16=f16_blob(emb),
            emb_q=None,
            emb_dim=dim,
            source="turn",
        )
        wrote["surface"] += 1

    deep_signal = bool(
        re.search(r"(remember|覚えて|重要|must|always|goal|制約|方針|ルール)", user_text, re.IGNORECASE)
        or re.search(r"(preference|好み|口調|性格|style)", user_text, re.IGNORECASE)
    )

    if deep_signal and not high_risk:
        deep_text = _deep_candidate(user_text, assistant_text)
        if deep_text:
            emb = embed_text(deep_text, dim)
            db.add_memory_item(
                session_key=session_key,
                layer="deep",
                text=deep_text,
                summary=deep_text[:220],
                importance=0.72,
                tags={"kind": "signal_deep", "ts": ts},
                emb_f16=f16_blob(emb),
                emb_q=quantize(emb, bits_per_dim),
                emb_dim=dim,
                source="turn",
            )
            wrote["deep"] += 1

    # Ephemeral for short low-value chatter.
    if len(user_text.strip()) <= 64 and not deep_signal:
        eph = _surface_summary(user_text, assistant_text)
        if eph:
            emb = embed_text(eph, dim)
            db.add_memory_item(
                session_key=session_key,
                layer="ephemeral",
                text=eph,
                summary=eph[:120],
                importance=0.3,
                tags={"kind": "ephemeral", "ts": ts},
                emb_f16=f16_blob(emb),
                emb_q=None,
                emb_dim=dim,
                ttl_expires_at=ts + 86400,
                source="turn",
            )
            wrote["ephemeral"] += 1

    # Keep surface bounded.
    db.trim_layer_size("surface", session_key, max_items=240)

    return wrote
