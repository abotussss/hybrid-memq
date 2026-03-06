from __future__ import annotations

import re
from typing import Any

from sidecar.memq.brain.service import BrainService
from sidecar.memq.config import Config


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+"),
]
OVERRIDE_PATTERNS = [
    re.compile(r"ignore (?:all )?previous instructions", re.IGNORECASE),
    re.compile(r"reveal (?:the )?(?:system|developer) prompt", re.IGNORECASE),
    re.compile(r"show me (?:the )?api key", re.IGNORECASE),
]
SCRIPT_BLOCKS = {
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff]"),
    "en": re.compile(r"[A-Za-z]"),
}



def _detect_languages(text: str) -> set[str]:
    langs: set[str] = set()
    for lang, pattern in SCRIPT_BLOCKS.items():
        if pattern.search(text):
            langs.add(lang)
    return langs


async def audit_output(
    *,
    cfg: Config,
    brain: BrainService,
    session_key: str,
    text: str,
    allowed_languages: list[str] | None,
    mode: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    risk = 0.0
    redacted = str(text or "")
    for pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
            reasons.append("secret_redacted")
            risk = max(risk, 0.95)
    for pattern in OVERRIDE_PATTERNS:
        if pattern.search(text):
            reasons.append("override_attempt")
            risk = max(risk, 0.8)
    allowed = allowed_languages or list(cfg.audit.allowed_languages_default)
    seen_langs = _detect_languages(text)
    unexpected = sorted(lang for lang in seen_langs if lang not in allowed)
    if unexpected:
        reasons.append(f"language_violation:{','.join(unexpected)}")
        risk = max(risk, 0.45)
    if mode == "dual" and cfg.audit.secondary_enabled and reasons and risk >= cfg.audit.risk_threshold:
        try:
            plan, _, _ = await brain.build_audit_patch(session_key=session_key, text=redacted, reasons=reasons)
            if plan.patched_text.strip():
                redacted = plan.patched_text
        except Exception:
            pass
    block = risk >= cfg.audit.block_threshold
    return {
        "ok": True,
        "risk": risk,
        "block": block,
        "redactedText": redacted if redacted != text or block else (redacted if reasons else text),
        "reasons": reasons,
    }
