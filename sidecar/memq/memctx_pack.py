from __future__ import annotations

from sidecar.memq.brain.schemas import BrainRecallPlan
from sidecar.memq.retrieval import RetrievalBundle
from sidecar.memq.tokens import estimate_tokens, fit_lines
import re


STYLE_ORDER = ["firstPerson", "callUser", "persona", "tone", "speaking_style", "verbosity", "prefix"]
STYLE_KEYS = set(STYLE_ORDER)
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")
MEMCTX_PREFIXES = ("wm.", "p.snapshot", "t.", "s", "d", "e")

PUBLIC_LABEL_REWRITES = (
    ("MEMRULES", "QRULE"),
    ("MEMRULE", "QRULE"),
    ("MEMSTYLE", "QSTYLE"),
    ("MEMCTX", "QCTX"),
)
TECHNICAL_ANCHOR_VALUES = {
    "memory-lancedb-pro",
    "memory-lancedb-pro-adapted",
    "lancedb",
    "sqlite",
    "qctx",
    "qstyle",
    "qrule",
}

FORBIDDEN_QCTX_FACT_PREFIXES = ("qstyle.", "qrule.")



def _compact(text: str, limit: int = 120) -> str:
    return " ".join(str(text or "").split())


def _looks_machine_key(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean or " " in clean:
        return False
    if clean.startswith(("http://", "https://")):
        return False
    return "." in clean or "_" in clean


def _query_text(plan: BrainRecallPlan) -> str:
    return " ".join([*plan.fts_queries, *plan.fact_keys])


def _line_overlap(plan: BrainRecallPlan, text: str) -> float:
    query_tokens = set(re.findall(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff_-]+", _query_text(plan).lower()))
    if not query_tokens:
        return 0.0
    text_tokens = set(re.findall(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff_-]+", str(text or "").lower()))
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / max(1, min(len(query_tokens), 4))


def _strip_mem_block_tags(text: str) -> str:
    out = str(text or "")
    for tag in ("MEMRULES", "MEMSTYLE", "MEMCTX", "QRULE", "QSTYLE", "QCTX"):
        start_token = f"<{tag}"
        end_token = f"</{tag}>"
        while True:
            start = out.find(start_token)
            if start < 0:
                break
            end = out.find(end_token, start)
            if end < 0:
                out = out[:start]
                break
            out = (out[:start] + " " + out[end + len(end_token):]).strip()
    return out


def _sanitize_mem_value(text: str, key: str = "") -> str:
    raw = _strip_mem_block_tags(str(text or "")).replace("\n", " ").replace("\r", " ")
    raw = raw.strip()
    if key and raw.startswith(f"{key}="):
        raw = raw[len(key) + 1 :].strip()
    segments = [segment.strip() for segment in raw.split("|")]
    kept: list[str] = []
    for segment in segments:
        if not segment:
            continue
        lowered = segment.lower()
        if "budget_tokens=" in lowered:
            continue
        if "budget:" in lowered or "budget=" in lowered:
            continue
        if "memrule_budget" in lowered or "memstyle_budget" in lowered or "memctx_budget" in lowered:
            continue
        if "<mem" in lowered or "</mem" in lowered:
            continue
        if "…" in segment:
            continue
        if segment.endswith(":") or segment.endswith("="):
            continue
        if lowered.startswith("profile.identity.card:"):
            _, _, value = segment.partition(":")
            if len(value.strip()) < 3:
                continue
        kept.append(" ".join(segment.split()))
    cleaned = " | ".join(kept)
    for old, new in PUBLIC_LABEL_REWRITES:
        cleaned = cleaned.replace(old, new)
    return cleaned


def _humanize_deep_anchor(text: str) -> str:
    segments = [segment.strip() for segment in str(text or "").split("|")]
    human: list[str] = []
    for segment in segments:
        if not segment:
            continue
        head, sep, tail = segment.partition(":")
        if sep and tail.strip() and _looks_machine_key(head):
            candidate = " ".join(tail.split())
        else:
            candidate = " ".join(segment.split())
        lowered = candidate.lower()
        if lowered in TECHNICAL_ANCHOR_VALUES:
            continue
        if len(candidate) < 6 and lowered not in {"ヒロ", "僕"}:
            continue
        human.append(candidate)
    human = _dedupe_consecutive_texts(human)
    cleaned = " | ".join(human)
    for old, new in PUBLIC_LABEL_REWRITES:
        cleaned = cleaned.replace(old, new)
    return cleaned


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


def _forbidden_qctx_payload(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(prefix in lowered for prefix in FORBIDDEN_QCTX_FACT_PREFIXES)


def _filter_memctx_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        key, _, _ = line.partition("=")
        if _valid_memctx_key(key.strip()) and not _forbidden_qctx_payload(line):
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
    kept: list[str] = []
    remaining: list[str] = []
    consumed = 0
    used = 0
    for line in lines:
        cost = _line_cost(line)
        if cost > budget_tokens or used + cost > budget_tokens:
            remaining.append(line)
            continue
        kept.append(line)
        used += cost
        consumed += cost
    return kept, remaining, consumed


def _dedupe_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for line in lines:
        clean = " ".join(str(line or "").split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _dedupe_consecutive_texts(values: list[str]) -> list[str]:
    out: list[str] = []
    previous = ""
    for value in values:
        clean = " ".join(str(value or "").split())
        if not clean:
            continue
        marker = clean.lower()
        if marker == previous:
            continue
        out.append(clean)
        previous = marker
    return out


def _compress_pipe_segments(text: str) -> str:
    segments = [segment.strip() for segment in str(text or "").split("|")]
    return " | ".join(_dedupe_consecutive_texts(segments))


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
    lines: list[str] = []
    for key in sorted(rules):
        if _valid_rule_key(key):
            lines.append(f"{key}={rules[key]}")
    return "\n".join(fit_lines(lines, budget_tokens))



def build_memstyle(style: dict[str, str], budget_tokens: int) -> str:
    lines: list[str] = []
    for key in STYLE_ORDER:
        value = style.get(key)
        if value:
            lines.append(f"{key}={_compact(value, 96)}")
    for key in sorted(style):
        if key in STYLE_ORDER or not _valid_style_key(key):
            continue
        lines.append(f"{key}={_compact(style[key], 96)}")
    return "\n".join(fit_lines(lines, budget_tokens))



def _profile_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    if float(plan.intent.profile) < 0.25:
        return lines
    snapshot = _sanitize_mem_value(bundle.anchors.get("p.snapshot", ""), "p.snapshot")
    if snapshot:
        lines.append(f"p.snapshot={_compact(snapshot, 160)}")
    return lines


def _deep_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    deep_items = list(bundle.deep)
    if plan.intent.timeline >= max(plan.intent.profile, plan.intent.fact):
        deep_items = [
            item
            for item in deep_items
            if (item.fact_key or "").startswith("timeline.") or _line_overlap(plan, f"{item.fact_key} {item.value} {item.summary}") >= 0.18
        ]
    elif plan.intent.profile >= max(plan.intent.timeline, plan.intent.fact):
        filtered: list = []
        for item in deep_items:
            fact_key = str(item.fact_key or "").lower()
            overlap = _line_overlap(plan, f"{item.fact_key} {item.value} {item.summary}")
            if fact_key.startswith("profile."):
                if item.fact_key in plan.fact_keys or fact_key in {
                    "profile.identity.card",
                    "profile.name",
                    "profile.display_name",
                    "profile.alias",
                    "profile.nickname",
                    "profile.user_name",
                } or overlap >= 0.18:
                    filtered.append(item)
            elif overlap >= 0.18:
                filtered.append(item)
        deep_items = filtered
    elif plan.intent.fact >= max(plan.intent.timeline, plan.intent.profile):
        deep_items = [
            item
            for item in deep_items
            if item.fact_key in plan.fact_keys
            or _line_overlap(plan, f"{item.fact_key} {item.value} {item.summary}") >= 0.16
            or not str(item.fact_key or "").startswith("profile.")
        ]
    deep_items = sorted(
        deep_items,
        key=lambda item: (item.score + _line_overlap(plan, f"{item.fact_key} {item.value} {item.summary}"), item.updated_at),
        reverse=True,
    )
    for idx, item in enumerate(deep_items[:4], start=1):
        key = _sanitize_mem_value(item.fact_key or f"d{idx}")
        payload = _sanitize_mem_value(item.value or item.summary)
        if not payload:
            continue
        if _forbidden_qctx_payload(key) or _forbidden_qctx_payload(payload):
            continue
        lowered_key = key.lower()
        lowered_payload = payload.lower()
        if "budget" in lowered_key or "budget_tokens=" in lowered_payload:
            continue
        if "memstyle_budget" in lowered_key or "memrule_budget" in lowered_key or "memctx_budget" in lowered_key:
            continue
        lines.append(f"d{idx}={_compact(key + ':' + payload, 160)}")
    return lines


def _timeline_fallback_detail(plan: BrainRecallPlan, bundle: RetrievalBundle) -> str:
    if bundle.timeline:
        summaries = _dedupe_consecutive_texts([
            _compact(str(item.get("summary", "")), 80)
            for item in bundle.timeline[:2]
            if item.get("summary")
        ])
        digest = " | ".join(summaries)
        if digest:
            return f"t.digest={digest}"
    recent = bundle.anchors.get("t.recent", "")
    if recent:
        return f"t.digest={_compact(_compress_pipe_segments(recent), 96)}"
    for item in bundle.deep:
        if (item.fact_key or "").startswith("timeline.") and (item.value or item.summary):
            return f"t.digest={_compact(item.value or item.summary, 96)}"
    return ""



def _timeline_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    recent = bundle.anchors.get("t.recent", "")
    recent = _compress_pipe_segments(_sanitize_mem_value(recent, "t.recent"))
    recent_line = f"t.recent={_compact(recent, 180)}" if recent else ""
    if plan.time_range is not None:
        lines.append(f"t.range={plan.time_range.start_day}..{plan.time_range.end_day}")
        lines.append(f"t.label={plan.time_range.label}")
        recent_line = ""
    digest_line = ""
    if bundle.timeline:
        event_summaries = _dedupe_consecutive_texts([
            _compact(str(item.get("summary", "")), 80)
            for item in bundle.timeline[:4]
            if item.get("summary")
        ])
        digest = " | ".join(event_summaries)
        if digest:
            digest_line = f"t.digest={digest}"
        if len(event_summaries) > 1:
            for idx, summary in enumerate(event_summaries, start=1):
                lines.append(f"t.ev{idx}={_compact(summary, 100)}")
    elif recent:
        digest_line = f"t.digest={_compact(recent, 96)}"
    if digest_line and recent_line:
        digest_value = digest_line.split("=", 1)[1]
        recent_value = recent_line.split("=", 1)[1]
        if digest_value == recent_value:
            recent_line = ""
    if recent_line:
        lines.append(recent_line)
    if digest_line:
        lines.append(digest_line)
    return lines



def _surface_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    surf = _sanitize_mem_value(bundle.anchors.get("wm.surf", ""), "wm.surf")
    if surf:
        lines.append(f"wm.surf={_compact(surf, 140)}")
    deep = _humanize_deep_anchor(_sanitize_mem_value(bundle.anchors.get("wm.deep", ""), "wm.deep"))
    lowered_deep = deep.lower()
    if deep and not (plan.intent.timeline >= max(plan.intent.profile, plan.intent.fact) and ("timeline." in lowered_deep or "task_" in lowered_deep)):
        lines.append(f"wm.deep={_compact(deep, 140)}")
    seen_summaries: set[str] = set()
    slot = 1
    for item in bundle.surface:
        summary = _compact(item.summary, 110)
        if not summary:
            continue
        if _forbidden_qctx_payload(summary):
            continue
        marker = summary.lower()
        if marker in seen_summaries:
            continue
        seen_summaries.add(marker)
        lines.append(f"s{slot}={summary}")
        slot += 1
        if slot > 3:
            break
    return lines


def _required_anchor_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> tuple[list[str], list[str]]:
    anchors_by_key: dict[str, str] = {}
    surf = _sanitize_mem_value(bundle.anchors.get("wm.surf", ""), "wm.surf")
    if surf:
        anchors_by_key["wm.surf"] = f"wm.surf={_compact(surf, 140)}"
    snapshot = _sanitize_mem_value(bundle.anchors.get("p.snapshot", ""), "p.snapshot")
    if snapshot and float(plan.intent.profile) >= 0.25:
        anchors_by_key["p.snapshot"] = f"p.snapshot={_compact(snapshot, 160)}"
    recent = _compress_pipe_segments(_sanitize_mem_value(bundle.anchors.get("t.recent", ""), "t.recent"))
    if recent and float(plan.intent.timeline) >= 0.25:
        anchors_by_key["t.recent"] = f"t.recent={_compact(recent, 180)}"
    if plan.intent.timeline >= max(plan.intent.profile, plan.intent.state + plan.intent.overview, plan.intent.fact):
        order = ["t.recent", "wm.surf", "p.snapshot"]
    elif plan.intent.profile >= max(plan.intent.timeline, plan.intent.state + plan.intent.overview, plan.intent.fact):
        order = ["p.snapshot", "wm.surf", "t.recent"]
    elif plan.intent.fact >= max(plan.intent.timeline, plan.intent.profile, plan.intent.state + plan.intent.overview):
        order = ["wm.surf", "p.snapshot", "t.recent"]
    else:
        order = ["wm.surf", "p.snapshot", "t.recent"]
    primary = [anchors_by_key[order[0]]] if order and order[0] in anchors_by_key else []
    secondary = [anchors_by_key[key] for key in order[1:] if key in anchors_by_key]
    return primary, secondary



def build_memctx(plan: BrainRecallPlan, bundle: RetrievalBundle, budget_tokens: int) -> str:
    available = max(0, budget_tokens)

    selected: list[str] = []
    used = 0
    seen = set()
    primary_anchors, secondary_anchors = _required_anchor_lines(plan, bundle)
    sections = {
        "profile": [line for line in _profile_lines(plan, bundle) if line not in seen],
        "timeline": [line for line in _timeline_lines(plan, bundle) if line not in seen],
        "surface": [line for line in _surface_lines(plan, bundle) if line not in seen],
        "deep": [line for line in _deep_lines(plan, bundle) if line not in seen],
        "ephemeral": [],
    }
    timeline_digest = next((line for line in sections["timeline"] if line.startswith("t.digest=")), "")
    has_timeline_detail = bool(timeline_digest or any(line.startswith("t.ev") for line in sections["timeline"]))
    if has_timeline_detail:
        digest_value = timeline_digest.split("=", 1)[1] if timeline_digest else ""
        primary_anchors = [
            line
            for line in primary_anchors
            if not line.startswith("t.recent=")
        ]
        secondary_anchors = [
            line
            for line in secondary_anchors
            if not line.startswith("t.recent=")
        ]
    dominant = _dominant_intent(plan)
    if dominant == "timeline":
        protected_timeline = [
            line
            for line in sections["timeline"]
            if line.startswith("t.range=") or line.startswith("t.label=")
        ]
        for line in protected_timeline:
            if line in seen:
                continue
            if _fits(line, available - used):
                selected.append(line)
                used += _line_cost(line)
                seen.add(line)
        sections["timeline"] = [line for line in sections["timeline"] if line not in protected_timeline]
        priority_timeline = next((line for line in sections["timeline"] if line.startswith("t.digest=") or line.startswith("t.ev")), "")
        if priority_timeline and _fits(priority_timeline, available - used):
            selected.append(priority_timeline)
            used += _line_cost(priority_timeline)
            seen.add(priority_timeline)
            sections["timeline"] = [line for line in sections["timeline"] if line != priority_timeline]
    for line in primary_anchors:
        if line in seen:
            continue
        if _fits(line, available - used):
            selected.append(line)
            used += _line_cost(line)
            seen.add(line)
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
        if line.startswith("t.recent=") and any(item.startswith("t.digest=") or item.startswith("t.ev") for item in selected):
            continue
        if _fits(line, remaining_budget):
            selected.append(line)
            remaining_budget -= _line_cost(line)
            seen.add(line)

    selected = _filter_memctx_lines(selected)
    selected = _dedupe_lines(selected)
    if dominant == "timeline" and plan.time_range is not None and not any(line.startswith("t.digest=") or line.startswith("t.ev") for line in selected):
        fallback_detail = _timeline_fallback_detail(plan, bundle)
        if fallback_detail:
            fallback_value = fallback_detail.split("=", 1)[1] if "=" in fallback_detail else fallback_detail
            if not any(line.startswith("t.recent=") and line.split("=", 1)[1] == fallback_value for line in selected):
                selected.append(fallback_detail)
            current_budget = budget_tokens
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
    final_lines = fit_lines(_dedupe_lines(selected), budget_tokens)
    if not final_lines:
        return ""
    return "\n".join(final_lines)



def compose_blocks(memrules: str, memstyle: str, memctx: str) -> str:
    blocks: list[str] = []
    if memrules.strip():
        blocks.append(f"<QRULE v1>\n{memrules}\n</QRULE>")
    if memstyle.strip():
        blocks.append(f"<QSTYLE v1>\n{memstyle}\n</QSTYLE>")
    if memctx.strip():
        blocks.append(f"<QCTX v1>\n{memctx}\n</QCTX>")
    return "\n\n".join(blocks)
