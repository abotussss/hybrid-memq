from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .config import MemqConfig
from .db import MemqDB
from .rules import extract_allowed_languages_from_rules


SECRET_PATTERNS = [
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|PRIVATE) KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}\.[A-Za-z0-9._-]{10,}\b"),
]

RISK_PATTERNS = [
    re.compile(r"ignore\s+previous", re.IGNORECASE),
    re.compile(r"reveal\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"api\s*key", re.IGNORECASE),
]


SCRIPT_MAP = {
    "latin": lambda ch: ("a" <= ch.lower() <= "z"),
    "kana": lambda ch: ("\u3040" <= ch <= "\u30ff"),
    "han": lambda ch: ("\u4e00" <= ch <= "\u9fff"),
    "hangul": lambda ch: ("\uac00" <= ch <= "\ud7af"),
    "cyrillic": lambda ch: ("\u0400" <= ch <= "\u04ff"),
    "arabic": lambda ch: ("\u0600" <= ch <= "\u06ff"),
}

LANG_TO_ALLOWED_SCRIPTS = {
    "en": {"latin"},
    "ja": {"latin", "kana", "han"},
    "zh": {"latin", "han"},
    "ko": {"latin", "hangul"},
    "ru": {"latin", "cyrillic"},
    "ar": {"latin", "arabic"},
}


@dataclass
class AuditResult:
    risk: float
    block: bool
    redacted_text: str
    reasons: List[str]


def _text_scripts(text: str) -> Set[str]:
    scripts: Set[str] = set()
    for ch in text:
        for name, pred in SCRIPT_MAP.items():
            if pred(ch):
                scripts.add(name)
    return scripts


def _allowed_scripts_for_langs(langs: List[str]) -> Set[str]:
    allowed: Set[str] = set()
    for lang in langs:
        allowed |= LANG_TO_ALLOWED_SCRIPTS.get(lang, {"latin"})
    allowed.add("latin")
    return allowed


def _secret_redact(text: str) -> Tuple[str, int]:
    out = text
    hits = 0
    for p in SECRET_PATTERNS:
        out2, n = p.subn("[REDACTED_SECRET]", out)
        if n > 0:
            hits += n
            out = out2
    return out, hits


def _language_policy_check(text: str, allowed_langs: List[str]) -> Tuple[bool, str]:
    scripts = _text_scripts(text)
    allowed = _allowed_scripts_for_langs(allowed_langs)
    disallowed = sorted([s for s in scripts if s not in allowed])
    if disallowed:
        return False, f"disallowed_scripts={','.join(disallowed)}"
    return True, ""


def _llm_repair(config: MemqConfig, text: str, allowed_langs: List[str], primary_lang: str) -> Optional[str]:
    if not config.llm_audit_enabled:
        return None
    if not config.llm_audit_api_key:
        return None

    prompt = (
        "You are an output safety/format auditor. "
        "Keep structure and meaning, but minimally rewrite risky spans only. "
        "Do not add/remove sections unless needed for safety/language policy. "
        f"Allowed languages: {','.join(allowed_langs)}. Primary language: {primary_lang}. "
        "If secrets/tokens are present, replace only that span with [REDACTED_SECRET]. "
        "Return only the revised text."
    )

    body = {
        "model": config.llm_audit_model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        config.llm_audit_url,
        method="POST",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.llm_audit_api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=config.llm_audit_timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            choices = payload.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                text_bits = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_bits.append(str(block.get("text", "")))
                merged = "".join(text_bits).strip()
                if merged:
                    return merged
    except Exception:
        return None
    return None


def audit_output(
    *,
    db: MemqDB,
    config: MemqConfig,
    session_key: str,
    text: str,
    mode: str,
    llm_audit_threshold: float,
    block_threshold: float,
) -> AuditResult:
    reasons: List[str] = []
    risk = 0.0
    out = text

    out, secret_hits = _secret_redact(out)
    if secret_hits > 0:
        risk += min(1.0, 0.55 + 0.1 * secret_hits)
        reasons.append("secret_pattern")

    for pat in RISK_PATTERNS:
        if pat.search(text):
            risk += 0.2
            reasons.append("override_or_exfil_phrase")
            break

    allowed_langs = extract_allowed_languages_from_rules(db)
    if not allowed_langs:
        allowed_langs = ["ja", "en"]
    policy_ok, policy_reason = _language_policy_check(out, allowed_langs)
    if not policy_ok:
        risk += 0.35
        reasons.append(f"language_policy:{policy_reason}")

    risk = min(1.0, risk)
    block = risk >= float(block_threshold)

    # Dual audit only when risk high enough.
    if mode == "dual" and risk >= float(llm_audit_threshold):
        primary = "ja" if "ja" in allowed_langs else allowed_langs[0]
        repaired = _llm_repair(config, out, allowed_langs, primary)
        if repaired and repaired.strip():
            out = repaired
            # re-run deterministic checks on repaired text
            out, _ = _secret_redact(out)
            ok2, _ = _language_policy_check(out, allowed_langs)
            if ok2 and "language_policy" in " ".join(reasons):
                reasons.append("dual_repair_applied")
                risk = max(0.0, risk - 0.2)
                block = risk >= float(block_threshold)

    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    db.add_audit_event(session_key=session_key, risk=risk, block=block, reasons=reasons, sample_hash=digest)

    if block and not out.strip():
        out = "[BLOCKED_BY_MEMRULES]"

    return AuditResult(risk=risk, block=block, redacted_text=out, reasons=reasons)
