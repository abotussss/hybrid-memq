from __future__ import annotations

import re
from typing import Dict


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def infer_intent(prompt: str) -> Dict[str, float]:
    p = str(prompt or "")
    low = p.lower()

    profile = 0.0
    timeline = 0.0
    state = 0.0
    fact = 0.0
    meta = 0.0
    overview = 0.0

    if re.search(r"(君は誰|あなたは誰|何者|who are you|what are you|自己紹介|identity)", p, re.IGNORECASE):
        profile += 0.95
    if re.search(r"(家族|family|妻|夫|子ども|子供|息子|娘|ペット|犬|猫|呼称|呼び方|一人称|好み|嗜好|人格|性格|口調|style|persona)", p, re.IGNORECASE):
        profile += 0.7

    if re.search(
        r"(昨日|一昨日|今日|今朝|昨晩|最近|直近|この前|前回|さっき|先週|先月|yesterday|recent|today|last week|last month|days? ago|日前)",
        p,
        re.IGNORECASE,
    ):
        timeline += 0.9

    if re.search(r"(どこまで|進捗|現状|今何|今の状態|status|progress|current state)", p, re.IGNORECASE):
        state += 0.8

    if re.search(r"(とは|って何|は何|what is|which|どれ|値|value|設定|set to|config)", p, re.IGNORECASE):
        fact += 0.65

    if re.search(r"(件数|count|stats|統計|プール|pool|何件|how many)", p, re.IGNORECASE):
        meta += 0.9

    if re.search(r"(要点|まとめ|要約|覚えてる|記憶|これまで|overview|summary|recap)", p, re.IGNORECASE):
        overview += 0.85

    # Short and abstract prompts are usually hard lexical matches; bias toward
    # overview/state to keep conversational continuity.
    plain_len = len(re.sub(r"\s+", "", p))
    if plain_len <= 24 and re.search(r"[?？]$", p.strip()):
        overview += 0.2
        state += 0.15

    if re.search(r"(あなた|君|お前|you)\s*(は|が)?", low, re.IGNORECASE):
        profile += 0.15

    return {
        "profile": _clamp01(profile),
        "timeline": _clamp01(timeline),
        "state": _clamp01(state),
        "fact_lookup": _clamp01(fact),
        "meta": _clamp01(meta),
        "overview": _clamp01(overview),
    }

