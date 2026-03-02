from __future__ import annotations

import random
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.memq.db import MemqDB
from sidecar.memq.timeline import date_to_day_key, day_key_from_ts, day_key_to_date, today_day_key


def main() -> None:
    random.seed(7)
    with tempfile.TemporaryDirectory() as d:
        db = MemqDB(Path(d) / "timeline-scale.sqlite3")
        try:
            now = int(time.time())
            today = day_key_to_date(today_day_key(now))
            rows = []
            total = 50_000
            for i in range(total):
                dd = today - timedelta(days=(i % 30))
                ts = int(time.mktime(dd.timetuple())) + (i % 86400)
                day_key = date_to_day_key(dd)
                rows.append(
                    (
                        str(uuid.uuid4()),
                        "s1",
                        ts,
                        day_key,
                        "assistant",
                        "action" if (i % 5 == 0) else "chat",
                        f"event-{i} topic={(i % 97)}",
                        "{}",
                        0.2 + (0.8 * random.random()),
                        None,
                        now,
                    )
                )
            db.conn.executemany(
                """
                INSERT INTO events(
                  id,session_key,ts,day_key,actor,kind,summary,tags_json,importance,ttl_expires_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            db.conn.commit()

            y_day = day_key_from_ts(now - 86400)
            t0 = time.perf_counter()
            out = db.list_events_range(
                session_key="s1",
                start_day=y_day,
                end_day=y_day,
                limit=200,
                include_global=True,
            )
            ms = (time.perf_counter() - t0) * 1000.0
            plan = db.conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM events
                WHERE day_key >= ? AND day_key <= ?
                  AND (session_key=? OR session_key='global')
                  AND (ttl_expires_at IS NULL OR ttl_expires_at > ?)
                ORDER BY day_key DESC, importance DESC, ts DESC
                LIMIT ?
                """,
                (y_day, y_day, "s1", now, 200),
            ).fetchall()
            plan_text = " | ".join(str(dict(x).get("detail", "")) for x in plan)
            print(f"timeline_scale_check: events={total} yesterday_hits={len(out)} query_ms={ms:.2f}")
            print(f"timeline_scale_check: plan={plan_text}")
            if len(out) == 0:
                raise SystemExit("FAIL: no events returned for yesterday")
            print("timeline_scale_check: PASS")
        finally:
            db.close()


if __name__ == "__main__":
    main()
