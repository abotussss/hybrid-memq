from __future__ import annotations


def estimate_tokens(text: str) -> int:
    clean = " ".join(str(text or "").split())
    if not clean:
        return 0
    return max(1, (len(clean) + 3) // 4)


def fit_lines(lines: list[str], budget_tokens: int) -> list[str]:
    out: list[str] = []
    used = 0
    for line in lines:
        line = " ".join(str(line or "").split())
        if not line:
            continue
        cost = estimate_tokens(line) + 1
        if cost > budget_tokens:
            continue
        if used + cost > budget_tokens:
            continue
        out.append(line)
        used += cost
    return out
