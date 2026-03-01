from __future__ import annotations

import re

_MEM_HEADER_RE = re.compile(r"^\s*(?:<|\[)MEM(?:RULES|STYLE|CTX)\s+v1(?:>|\])\s*$", re.IGNORECASE)
_MEM_FOOTER_RE = re.compile(r"^\s*</MEM(?:RULES|STYLE|CTX)\s+v1>\s*$", re.IGNORECASE)
_KV_LINE_RE = re.compile(r"^\s*[A-Za-z0-9_.-]{1,64}\s*=\s*.*$")


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
