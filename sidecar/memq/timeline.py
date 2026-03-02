from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


TOKYO_TZ = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True)
class TimelineRange:
    start_day: str
    end_day: str
    label: str
    explicit: bool


def day_key_from_ts(ts: int, tz: ZoneInfo = TOKYO_TZ) -> str:
    return datetime.fromtimestamp(int(ts), tz).date().isoformat()


def today_day_key(now_ts: int | None = None, tz: ZoneInfo = TOKYO_TZ) -> str:
    ts = int(now_ts or time.time())
    return day_key_from_ts(ts, tz=tz)


def day_key_to_date(day_key: str) -> date:
    return date.fromisoformat(day_key)


def date_to_day_key(d: date) -> str:
    return d.isoformat()


def iter_day_keys(start_day: str, end_day: str, max_days: int = 62) -> list[str]:
    s = day_key_to_date(start_day)
    e = day_key_to_date(end_day)
    if s > e:
        s, e = e, s
    out: list[str] = []
    cur = s
    while cur <= e and len(out) < max_days:
        out.append(date_to_day_key(cur))
        cur = cur + timedelta(days=1)
    return out


def detect_timeline_range(prompt: str, now_ts: int | None = None, tz: ZoneInfo = TOKYO_TZ) -> TimelineRange | None:
    p = str(prompt or "")
    if not p.strip():
        return None
    now = datetime.fromtimestamp(int(now_ts or time.time()), tz)
    today = now.date()
    low = p.lower()

    def one_day(target: date, label: str, explicit: bool = True) -> TimelineRange:
        k = date_to_day_key(target)
        return TimelineRange(start_day=k, end_day=k, label=label, explicit=explicit)

    if re.search(r"(一昨日|おととい|day before yesterday)", p, re.IGNORECASE):
        return one_day(today - timedelta(days=2), "day_before_yesterday")
    if re.search(r"(昨日|きのう|yesterday|昨晩|last night)", p, re.IGNORECASE):
        return one_day(today - timedelta(days=1), "yesterday")
    if re.search(r"(今日|きょう|today|今朝|this morning)", p, re.IGNORECASE):
        return one_day(today, "today")
    if re.search(r"(先週|last week)", p, re.IGNORECASE):
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
        return TimelineRange(date_to_day_key(start), date_to_day_key(end), "last_week", True)
    if re.search(r"(今週|this week)", p, re.IGNORECASE):
        start = today - timedelta(days=today.weekday())
        return TimelineRange(date_to_day_key(start), date_to_day_key(today), "this_week", True)
    if re.search(r"(先月|last month)", p, re.IGNORECASE):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return TimelineRange(date_to_day_key(first_prev), date_to_day_key(last_prev), "last_month", True)
    if re.search(r"(最近|ここんところ|latest|recently|these days)", p, re.IGNORECASE):
        start = today - timedelta(days=3)
        return TimelineRange(date_to_day_key(start), date_to_day_key(today), "recent", True)

    # Fallback for broad memory overview prompts: provide recent timeline context.
    if re.search(r"(何か覚えて|覚えてる|memory|remember|これまで|summary|要約)", low, re.IGNORECASE):
        start = today - timedelta(days=2)
        return TimelineRange(date_to_day_key(start), date_to_day_key(today), "recent_fallback", False)
    return None

