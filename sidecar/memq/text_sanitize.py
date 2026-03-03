from __future__ import annotations

import re

_MEM_HEADER_RE = re.compile(r"^\s*(?:<|\[)MEM(?:RULES|STYLE|CTX)\s+v1(?:>|\])\s*$", re.IGNORECASE)
_MEM_FOOTER_RE = re.compile(r"^\s*</MEM(?:RULES|STYLE|CTX)\s+v1>\s*$", re.IGNORECASE)
_KV_LINE_RE = re.compile(r"^\s*[A-Za-z0-9_.-]{1,64}\s*=\s*.*$")
_RUNTIME_NOISE_RE = re.compile(
    r"(thinkingSignature|encrypted_content|\"type\"\s*:\s*\"(?:reasoning|thinking)\"|"
    r"Conversation info \(untrusted metadata\)|function_call_output|tool_call_id|call_[A-Za-z0-9]{8,})",
    re.IGNORECASE,
)


def strip_memq_blocks(text: str) -> str:
    """Remove MEMQ injection blocks while preserving normal user/assistant text.

    Supported headers:
      - <MEMRULES v1>, <MEMSTYLE v1>, <MEMCTX v1>
      - [MEMRULES v1], [MEMSTYLE v1], [MEMCTX v1]

    Block body is treated as removable while lines are either blank or k=v.
    If a non-k=v line appears, the block is considered ended and that line is kept.
    """

    if not text:
        return ""

    out: list[str] = []
    in_block = False

    for ln in text.splitlines():
        if not in_block:
            if _MEM_HEADER_RE.match(ln):
                in_block = True
                continue
            out.append(ln)
            continue

        # in block
        s = ln.strip()
        if not s:
            continue
        if _MEM_FOOTER_RE.match(ln):
            in_block = False
            continue
        if _MEM_HEADER_RE.match(ln):
            in_block = True
            continue
        if _KV_LINE_RE.match(ln):
            continue

        # Non kv line: treat as normal text and end block.
        in_block = False
        out.append(ln)

    return "\n".join(out)


def contains_runtime_noise(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return bool(_RUNTIME_NOISE_RE.search(t))


def strip_runtime_noise(text: str) -> str:
    t = str(text or "")
    if not t:
        return ""
    lines: list[str] = []
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = _RUNTIME_NOISE_RE.search(s)
        if not m:
            lines.append(ln)
            continue
        # Keep only meaningful prefix if line is contaminated by runtime JSON fragments.
        left = s[: m.start()].strip(" ,|:-")
        if len(left) >= 10 and not left.endswith("{"):
            lines.append(left)
    t = "\n".join(lines)
    t = re.sub(r"\{[^{}]{0,1200}(thinkingSignature|encrypted_content)[^{}]{0,2400}\}", " ", t, flags=re.IGNORECASE)
    t = re.sub(r'\\?"(?:thinking|thinkingSignature|encrypted_content|type|id)\\?"\s*:\s*\\?"[^"]{0,1200}\\?"', " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\brs_[A-Za-z0-9]{20,}\b", " ", t)
    t = re.sub(r"\bgAAAA[A-Za-z0-9_\-]{20,}\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t
