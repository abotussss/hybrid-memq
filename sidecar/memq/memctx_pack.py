from __future__ import annotations

from sidecar.memq.brain.schemas import BrainRecallPlan
from sidecar.memq.retrieval import RetrievalBundle
from sidecar.memq.tokens import estimate_tokens, fit_lines
import re


STYLE_ORDER = ["firstPerson", "callUser", "persona", "tone", "speaking_style", "verbosity", "prefix"]
STYLE_KEYS = set(STYLE_ORDER)
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")
QCTX_PREFIXES = ("wm.", "p.snapshot", "t.", "s", "d", "e")

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


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", str(text or "")))


def _semantic_dedupe_values(values: list[str], threshold: float = 0.72) -> list[str]:
    kept: list[str] = []
    for value in values:
        clean = " ".join(str(value or "").split())
        if not clean:
            continue
        if any(_semantic_overlap_text(clean, existing) >= threshold for existing in kept):
            continue
        kept.append(clean)
    return kept


def _semantic_overlap_text(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[0-9A-Za-z぀-ヿ㐀-鿿_-]+", str(left or "").lower()))
    right_tokens = set(re.findall(r"[0-9A-Za-z぀-ヿ㐀-鿿_-]+", str(right or "").lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _prefer_event_payload(item: dict) -> str:
    raw = _sanitize_mem_value(item.get("text") or "")
    summary = _sanitize_mem_value(item.get("summary") or "")
    candidates = [candidate for candidate in (raw, summary) if candidate]
    if not candidates:
        return ""
    scored = []
    for candidate in candidates:
        score = 0.0
        if _contains_cjk(candidate):
            score += 2.0
        if "[chat]" not in candidate.lower() and "[digest]" not in candidate.lower():
            score += 0.6
        if len(candidate) >= 24:
            score += 0.2
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]



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
        if len(candidate) < 6 and not any(ch.isascii() is False for ch in candidate):
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
    return any(key == prefix or key.startswith(prefix) for prefix in QCTX_PREFIXES)


def _forbidden_qctx_payload(text: str) -> bool:
    lowered = str(text or "").lower().strip()
    if any(prefix in lowered for prefix in FORBIDDEN_QCTX_FACT_PREFIXES):
        return True
    return lowered.startswith(RULE_PREFIXES)


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


def _line_value(line: str) -> str:
    _, _, value = str(line or "").partition("=")
    return value.strip().lower()


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
        lines.append(f"p.snapshot={snapshot}")
    return lines


def _memory_payload_text(item) -> str:
    for candidate in (
        getattr(item, "text", ""),
        getattr(item, "value", ""),
        getattr(item, "summary", ""),
    ):
        cleaned = _sanitize_mem_value(candidate)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in TECHNICAL_ANCHOR_VALUES:
            continue
        if _looks_machine_key(cleaned) and " " not in cleaned and "。" not in cleaned and "、" not in cleaned:
            continue
        return cleaned
    return ""


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
        key=lambda item: (item.score + _line_overlap(plan, f"{item.fact_key} {getattr(item, 'text', '')} {item.value} {item.summary}"), item.updated_at),
        reverse=True,
    )
    for idx, item in enumerate(deep_items[:4], start=1):
        payload = _memory_payload_text(item)
        if not payload:
            continue
        if _forbidden_qctx_payload(payload):
            continue
        lowered_payload = payload.lower()
        if "budget_tokens=" in lowered_payload:
            continue
        if any(lowered_payload.startswith(prefix) for prefix in RULE_PREFIXES):
            continue
        lines.append(f"d{idx}={payload}")
    return lines



def _timeline_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    wants_timeline = plan.time_range is not None or float(plan.intent.timeline) >= 0.25 or bool(bundle.timeline)
    if not wants_timeline:
        return lines
    if plan.time_range is not None:
        lines.append(f"t.range={plan.time_range.start_day}..{plan.time_range.end_day}")
        lines.append(f"t.label={plan.time_range.label}")
    if bundle.timeline:
        timeline_items = _semantic_dedupe_values([
            _prefer_event_payload(item)
            for item in bundle.timeline[:6]
            if _prefer_event_payload(item)
        ])
        for idx, summary in enumerate(timeline_items[:4], start=1):
            cleaned = _sanitize_mem_value(summary)
            if cleaned and not _forbidden_qctx_payload(cleaned):
                lines.append(f"t.ev{idx}={cleaned}")
    if not any(line.startswith("t.ev") for line in lines):
        fallback_items = []
        surface_items = list(bundle.surface[:6])
        preferred_surface = [item for item in surface_items if str(getattr(item, "kind", "")).lower() != "digest"]
        if not preferred_surface:
            preferred_surface = surface_items
        for item in preferred_surface[:3]:
            payload = _memory_payload_text(item)
            if payload:
                fallback_items.append(payload)
        if not fallback_items:
            for item in bundle.deep[:2]:
                payload = _memory_payload_text(item)
                if payload and not _forbidden_qctx_payload(payload):
                    fallback_items.append(payload)
        for idx, payload in enumerate(_semantic_dedupe_values(fallback_items)[:3], start=1):
            cleaned = _sanitize_mem_value(payload)
            if cleaned and not _forbidden_qctx_payload(cleaned):
                lines.append(f"t.ev{idx}={cleaned}")
    return lines



def _surface_lines(plan: BrainRecallPlan, bundle: RetrievalBundle) -> list[str]:
    lines: list[str] = []
    seen_summaries: set[str] = set()
    slot = 1
    for item in bundle.surface:
        summary = _memory_payload_text(item)
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
    snapshot = _sanitize_mem_value(bundle.anchors.get("p.snapshot", ""), "p.snapshot")
    if snapshot and float(plan.intent.profile) >= 0.25:
        anchors_by_key["p.snapshot"] = f"p.snapshot={snapshot}"
    if plan.intent.timeline >= max(plan.intent.profile, plan.intent.state + plan.intent.overview, plan.intent.fact):
        order = ["p.snapshot"]
    elif plan.intent.profile >= max(plan.intent.timeline, plan.intent.state + plan.intent.overview, plan.intent.fact):
        order = ["p.snapshot"]
    elif plan.intent.fact >= max(plan.intent.timeline, plan.intent.profile, plan.intent.state + plan.intent.overview):
        order = ["p.snapshot"]
    else:
        order = ["p.snapshot"]
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
    has_timeline_detail = bool(any(line.startswith("t.ev") for line in sections["timeline"]))
    if has_timeline_detail:
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
        priority_timeline = next((line for line in sections["timeline"] if line.startswith("t.ev")), "")
        if priority_timeline and _fits(priority_timeline, available - used):
            selected.append(priority_timeline)
            used += _line_cost(priority_timeline)
            seen.add(priority_timeline)
            sections["timeline"] = [line for line in sections["timeline"] if line != priority_timeline]
    timeline_values = {_line_value(line) for line in selected if line.startswith("t.ev")}
    has_timeline_payload = bool(timeline_values) or any(line.startswith("t.range=") for line in selected)
    if timeline_values:
        sections["surface"] = [
            line for line in sections["surface"]
            if _line_value(line) not in timeline_values
            and max((_semantic_overlap_text(_line_value(line), value) for value in timeline_values), default=0.0) < 0.72
        ]
        sections["deep"] = [
            line for line in sections["deep"]
            if _line_value(line) not in timeline_values
            and max((_semantic_overlap_text(_line_value(line), value) for value in timeline_values), default=0.0) < 0.72
        ]
    if dominant == "timeline" and has_timeline_payload:
        sections["surface"] = []
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
        if line.startswith("t.recent=") and any(item.startswith("t.ev") for item in selected):
            continue
        if _fits(line, remaining_budget):
            selected.append(line)
            remaining_budget -= _line_cost(line)
            seen.add(line)

    selected = _filter_memctx_lines(selected)
    selected = _dedupe_lines(selected)
    timeline_payloads = [_line_value(line) for line in selected if line.startswith("t.ev")]
    if timeline_payloads:
        selected = [
            line for line in selected
            if not (
                (line.startswith("s") or line.startswith("d"))
                and max((_semantic_overlap_text(_line_value(line), payload) for payload in timeline_payloads), default=0.0) >= 0.72
            )
        ]
        selected = _dedupe_lines(selected)
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
