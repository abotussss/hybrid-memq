from __future__ import annotations

from sidecar.memq.brain.schemas import BrainRecallPlan
from sidecar.memq.retrieval import RetrievalBundle
from sidecar.memq.tokens import estimate_tokens, fit_lines


STYLE_ORDER = ["firstPerson", "callUser", "persona", "tone", "speaking_style", "verbosity", "prefix"]
STYLE_KEYS = set(STYLE_ORDER)
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")
MEMCTX_PREFIXES = ("wm.", "p.snapshot", "t.", "s", "d", "e")



def _compact(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _line_cost(line: str) -> int:
    return estimate_tokens(line) + 1


def _fits(line: str, remaining: int) -> bool:
    return _line_cost(line) <= remaining


def _valid_rule_key(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in RULE_PREFIXES)


def _valid_style_key(key: str) -> bool:
    return key in STYLE_KEYS


def _valid_memctx_key(key: str) -> bool:
    return any(key == prefix or key.startswith(prefix) for prefix in MEMCTX_PREFIXES)


def _filter_memctx_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        key, _, _ = line.partition("=")
        if _valid_memctx_key(key.strip()):
            out.append(line)
    return out


def _scaled_budgets(requested: dict[str, int], available: int) -> dict[str, int]:
    if available <= 0:
        return {key: 0 for key in requested}
    positive = {key: max(0, value) for key, value in requested.items()}
    total_requested = sum(positive.values())
    if total_requested <= 0:
        return {key: 0 for key in requested}
    if total_requested <= available:
        return positive
    scaled: dict[str, int] = {}
    remaining = available
    keys = [key for key, value in positive.items() if value > 0]
    for index, key in enumerate(keys):
        if index == len(keys) - 1:
            scaled[key] = remaining
            break
        share = int(available * positive[key] / total_requested)
        scaled[key] = min(remaining, max(0, share))
        remaining -= scaled[key]
    for key in requested:
        scaled.setdefault(key, 0)
    return scaled


def _take_lines(lines: list[str], budget_tokens: int) -> tuple[list[str], list[str], int]:
    if budget_tokens <= 0 or not lines:
        return [], list(lines), 0
    kept = fit_lines(lines, budget_tokens)
    consumed = sum(_line_cost(line) for line in kept)
    return kept, lines[len(kept) :], consumed


def _intent_priority(plan: BrainRecallPlan) -> list[str]:
    weights = {
        "timeline": float(plan.intent.timeline),
        "profile": float(plan.intent.profile),
        "surface": float(plan.intent.state + plan.intent.overview),
        "deep": float(plan.intent.fact),
        "ephemeral": 0.0,
    }
    return [name for name, _ in sorted(weights.items(), key=lambda item: item[1], reverse=True)]


def _dominant_intent(plan: BrainRecallPlan) -> str:
    return _intent_priority(plan)[0]



def build_memrules(rules: dict[str, str], budget_tokens: int) -> str:
    lines = ["budget_tokens=" + str(budget_tokens)]
    for key in sorted(rules):
        if _valid_rule_key(key):
            lines.append(f"{key}={rules[key]}")
    return "\n".join(fit_lines(lines, budget_tokens))



def build_memstyle(style: dict[str, str], budget_tokens: int) -> str:
    lines = ["budget_tokens=" + str(budget_tokens)]
    for key in STYLE_ORDER:
        value = style.get(key)
        if value:
            lines.append(f"{key}={_compact(value, 96)}")
    for key in sorted(style):
        if key in STYLE_ORDER or not _valid_style_key(key):
            continue
        lines.append(f"{key}={_compact(style[key], 96)}")
    return "\n".join(fit_lines(lines, budget_tokens))



def _profile_lines(bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    snapshot = bundle.anchors.get("p.snapshot", "")
    if snapshot:
        lines.append(f"p.snapshot={_compact(snapshot, 160)}")
    return lines


def _deep_lines(bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    profile_items = [item for item in bundle.deep if (item.fact_key or "").startswith("profile.")]
    other_items = [item for item in bundle.deep if not (item.fact_key or "").startswith("profile.")]
    deep_items = profile_items + other_items
    for idx, item in enumerate(deep_items[:4], start=1):
        key = item.fact_key or f"d{idx}"
        payload = item.value or item.summary
        if not payload:
            continue
        lines.append(f"d{idx}={_compact(key + ':' + payload, 160)}")
    return lines


def _timeline_fallback_detail(plan: BrainRecallPlan, bundle: RetrievalBundle) -> str:
    if bundle.timeline:
        digest = " | ".join(_compact(str(item.get("summary", "")), 80) for item in bundle.timeline[:2] if item.get("summary"))
        if digest:
            return f"t.digest={digest}"
    recent = bundle.anchors.get("t.recent", "")
    if recent:
        return f"t.digest={_compact(recent, 96)}"
    for item in bundle.deep:
        if (item.fact_key or "").startswith("timeline.") and (item.value or item.summary):
            return f"t.digest={_compact(item.value or item.summary, 96)}"
    return ""



def _timeline_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    recent = bundle.anchors.get("t.recent", "")
    if recent:
        lines.append(f"t.recent={_compact(recent, 180)}")
    if plan.time_range is not None:
        lines.append(f"t.range={plan.time_range.start_day}..{plan.time_range.end_day}")
        lines.append(f"t.label={plan.time_range.label}")
    if bundle.timeline:
        digest = " | ".join(_compact(str(item.get("summary", "")), 80) for item in bundle.timeline[:4] if item.get("summary"))
        if digest:
            lines.append(f"t.digest={digest}")
        for idx, item in enumerate(bundle.timeline[:4], start=1):
            lines.append(f"t.ev{idx}={_compact(str(item.get('summary', '')), 100)}")
    elif recent:
        lines.append(f"t.digest={_compact(recent, 96)}")
    return lines



def _surface_lines(bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    surf = bundle.anchors.get("wm.surf", "")
    if surf:
        lines.append(f"wm.surf={_compact(surf, 140)}")
    deep = bundle.anchors.get("wm.deep", "")
    if deep:
        lines.append(f"wm.deep={_compact(deep, 140)}")
    for idx, item in enumerate(bundle.surface[:3], start=1):
        lines.append(f"s{idx}={_compact(item.summary, 110)}")
    return lines


def _required_anchor_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> tuple[list[str], list[str]]:
    anchors_by_key: dict[str, str] = {}
    surf = bundle.anchors.get("wm.surf", "")
    if surf:
        anchors_by_key["wm.surf"] = f"wm.surf={_compact(surf, 140)}"
    snapshot = bundle.anchors.get("p.snapshot", "")
    if snapshot:
        anchors_by_key["p.snapshot"] = f"p.snapshot={_compact(snapshot, 160)}"
    recent = bundle.anchors.get("t.recent", "")
    if recent:
        anchors_by_key["t.recent"] = f"t.recent={_compact(recent, 180)}"
    if plan.intent.timeline >= max(plan.intent.profile, plan.intent.state + plan.intent.overview):
        order = ["t.recent", "wm.surf", "p.snapshot"]
    elif plan.intent.profile >= max(plan.intent.timeline, plan.intent.state + plan.intent.overview):
        order = ["p.snapshot", "wm.surf", "t.recent"]
    else:
        order = ["wm.surf", "p.snapshot", "t.recent"]
    primary = [anchors_by_key[order[0]]] if order and order[0] in anchors_by_key else []
    secondary = [anchors_by_key[key] for key in order[1:] if key in anchors_by_key]
    return primary, secondary



def build_memctx(plan: BrainRecallPlan, bundle: RetrievalBundle, budget_tokens: int) -> str:
    base_line = f"budget_tokens={budget_tokens}"
    available = max(0, budget_tokens - _line_cost(base_line))

    selected: list[str] = []
    used = 0
    seen = set()
    primary_anchors, secondary_anchors = _required_anchor_lines(plan, bundle)
    for line in primary_anchors:
        if line in seen:
            continue
        if _fits(line, available - used):
            selected.append(line)
            used += _line_cost(line)
            seen.add(line)

    sections = {
        "profile": [line for line in _profile_lines(bundle) if line not in seen],
        "timeline": [line for line in _timeline_lines(plan, bundle) if line not in seen],
        "surface": [line for line in _surface_lines(bundle) if line not in seen],
        "deep": [line for line in _deep_lines(bundle) if line not in seen],
        "ephemeral": [],
    }
    dominant = _dominant_intent(plan)
    if dominant == "timeline":
        priority_timeline = next((line for line in sections["timeline"] if line.startswith("t.digest=") or line.startswith("t.ev")), "")
        if priority_timeline and _fits(priority_timeline, available - used):
            selected.append(priority_timeline)
            used += _line_cost(priority_timeline)
            seen.add(priority_timeline)
            sections["timeline"] = [line for line in sections["timeline"] if line != priority_timeline]
    requested = {
        "profile": plan.budget_split.profile,
        "timeline": plan.budget_split.timeline,
        "surface": plan.budget_split.surface,
        "deep": plan.budget_split.deep,
        "ephemeral": plan.budget_split.ephemeral,
    }
    budgets = _scaled_budgets(requested, max(0, available - used))

    leftovers: dict[str, list[str]] = {}
    for key, lines in sections.items():
        kept, remaining, consumed = _take_lines(lines, budgets.get(key, 0))
        selected.extend(kept)
        used += consumed
        leftovers[key] = remaining

    remaining_budget = max(0, available - used)
    for key in _intent_priority(plan):
        if remaining_budget <= 0:
            break
        if key not in leftovers:
            continue
        kept, remaining, consumed = _take_lines(leftovers[key], remaining_budget)
        selected.extend(kept)
        leftovers[key] = remaining
        remaining_budget -= consumed

    for line in secondary_anchors:
        if line in seen:
            continue
        if _fits(line, remaining_budget):
            selected.append(line)
            remaining_budget -= _line_cost(line)
            seen.add(line)

    selected = _filter_memctx_lines(selected)
    if dominant == "timeline" and plan.time_range is not None and not any(line.startswith("t.digest=") or line.startswith("t.ev") for line in selected):
        fallback_detail = _timeline_fallback_detail(plan, bundle)
        if fallback_detail:
            selected.append(fallback_detail)
            current_budget = budget_tokens - _line_cost(base_line)
            protected_prefixes = ("t.recent=", "t.range=", "t.label=", "t.digest=", "t.ev")
            while sum(_line_cost(line) for line in selected) > current_budget:
                removable_index = next(
                    (index for index in range(len(selected) - 1, -1, -1) if not selected[index].startswith(protected_prefixes)),
                    None,
                )
                if removable_index is None:
                    break
                selected.pop(removable_index)
    if not selected:
        return ""
    final_lines = fit_lines([base_line] + selected, budget_tokens)
    if len(final_lines) <= 1:
        return ""
    return "\n".join(final_lines)



def compose_blocks(memrules: str, memstyle: str, memctx: str) -> str:
    blocks: list[str] = []
    if memrules.strip():
        blocks.append(f"<MEMRULES v1>\n{memrules}\n</MEMRULES>")
    if memstyle.strip():
        blocks.append(f"<MEMSTYLE v1>\n{memstyle}\n</MEMSTYLE>")
    if memctx.strip():
        blocks.append(f"<MEMCTX v1>\n{memctx}\n</MEMCTX>")
    return "\n\n".join(blocks)
