from __future__ import annotations

import re
from typing import Set


LATIN_RE = re.compile(r"[a-z0-9_]{2,}")
JP_SPAN_RE = re.compile(r"[ぁ-んァ-ヶ一-龠]{2,32}")


def tokenize_lexical(text: str) -> Set[str]:
    s = str(text or "").lower()
    out: Set[str] = set(LATIN_RE.findall(s))

    # CJK tokens: keep spans and add char n-grams to tolerate paraphrase like
    # "家族構成" <-> "家族" without embeddings.
    for span in JP_SPAN_RE.findall(s):
        span = span.strip()
        if not span:
            continue
        out.add(span)
        L = len(span)
        for n in (2, 3, 4):
            if L < n:
                continue
            for i in range(0, L - n + 1):
                out.add(span[i : i + n])
    return out


def lexical_overlap(query_tokens: Set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    t = tokenize_lexical(text)
    if not t:
        return 0.0
    return float(len(query_tokens & t)) / float(max(1, len(query_tokens)))

