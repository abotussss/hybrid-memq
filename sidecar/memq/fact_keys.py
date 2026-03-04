from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set, Tuple


# Centralized key taxonomy so query/text key inference is declarative and reusable.
_PATTERNS: Sequence[Tuple[re.Pattern[str], Sequence[str]]] = [
    (
        re.compile(r"(君は誰|あなたは誰|who are you|what are you|何者|自己紹介|identity)", re.IGNORECASE),
        ("profile.identity", "profile.persona.role"),
    ),
    (
        re.compile(r"((?:俺|私|僕|わたし|ぼく).{0,3}名前|my name|名前は)", re.IGNORECASE),
        ("profile.user.name",),
    ),
    (
        re.compile(r"(家族|family|妻|夫|husband|wife|子ども|子供|息子|娘|child|son|daughter|犬|猫|ペット|pet|dog|cat)", re.IGNORECASE),
        ("profile.family", "profile.family.summary", "profile.family_structure", "relationship.spouse", "relationship.wife"),
    ),
    (re.compile(r"(家族構成|family composition)", re.IGNORECASE), ("profile.family.summary", "profile.family_structure")),
    (re.compile(r"(妻|夫|husband|wife)", re.IGNORECASE), ("profile.family.spouse",)),
    (re.compile(r"(犬|猫|ペット|pet|dog|cat)", re.IGNORECASE), ("profile.family.pet",)),
    (re.compile(r"(子ども|子供|息子|娘|child|son|daughter)", re.IGNORECASE), ("profile.family.child",)),
    (re.compile(r"(子ども.*\d+人|\d+人.*子ども|children?\s*\d+)", re.IGNORECASE), ("profile.family.children_count",)),
    (re.compile(r"(人格|persona|キャラ|ロール|roleplay|口調|tone|話し方|speaking style)", re.IGNORECASE), ("profile.persona", "profile.persona.role")),
    (re.compile(r"(呼称|呼び方|call me|identity)", re.IGNORECASE), ("profile.identity", "profile.identity.call_user", "profile.calling_name")),
    (re.compile(r"(って呼んで|と呼んで|呼んでほしい)", re.IGNORECASE), ("profile.identity.call_user",)),
    (re.compile(r"(一人称|first person|firstPerson)", re.IGNORECASE), ("profile.identity", "profile.identity.first_person")),
    (re.compile(r"(検索設定|検索エンジン|search\s*settings?|search\s*engine)", re.IGNORECASE), ("pref.search.engine",)),
    (re.compile(r"(検索|search).*(brave|google|bing|duckduckgo)", re.IGNORECASE), ("pref.search.engine",)),
    (re.compile(r"(ルール|rule|policy|方針|制約|constraint)", re.IGNORECASE), ("memory.policy",)),
    (re.compile(r"(10分前|直近|recent|さっき|minutes? ago)", re.IGNORECASE), ("memory.recent",)),
    (
        re.compile(r"(覚えてる|記憶|これまで|要点|まとめ|recap|summary|what do you remember|memory)", re.IGNORECASE),
        ("memory.recent", "memory.note"),
    ),
    (
        re.compile(r"(タスク|task|todo|進捗|status|project|案件|計画|plan|deadline|期限)", re.IGNORECASE),
        ("project.task", "project.status", "memory.note"),
    ),
    (re.compile(r"(趣味|hobby|好き|好み|preference)", re.IGNORECASE), ("profile.preference",)),
]


def _dedupe(keys: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for k in keys:
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def infer_query_fact_keys(text: str) -> Set[str]:
    q = text or ""
    out: List[str] = []
    for pat, keys in _PATTERNS:
        if pat.search(q):
            out.extend(keys)
    return set(_dedupe(out))


def infer_text_fact_keys(text: str) -> List[str]:
    return _dedupe(infer_query_fact_keys(text))
