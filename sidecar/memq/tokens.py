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
        if out and used + cost > budget_tokens:
            break
        if not out and cost > budget_tokens:
            trimmed = line[: max(24, budget_tokens * 4)]
            out.append(trimmed)
            break
        out.append(line)
        used += cost
    return out
