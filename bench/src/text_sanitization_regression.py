from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.memq.db import MemqDB
from sidecar.memq.ingest import _sanitize_turn_text, ingest_turn
from sidecar.memq.text_sanitize import strip_memq_blocks


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_no_char_split() -> None:
    src = "こんにちは世界"
    out = _sanitize_turn_text(src)
    _assert("こ ん に ち は" not in out, "sanitize split JP characters")
    _assert("こんにちは世界" in out, "sanitize lost normal text")


def test_strip_mem_block_keep_user_text() -> None:
    src = "<MEMCTX v1>\nctx.a=b\n\nユーザー:こんにちは"
    out = strip_memq_blocks(src)
    _assert("<MEMCTX v1>" not in out, "header not removed")
    _assert("ctx.a=b" not in out, "kv line not removed")
    _assert("ユーザー:こんにちは" in out, "user line was removed")


def test_ingest_short_sentence_surface_ok() -> None:
    with tempfile.TemporaryDirectory() as d:
        db = MemqDB(Path(d) / "memq.sqlite3")
        try:
            wrote = ingest_turn(
                db=db,
                session_key="s1",
                user_text="こんにちは、これは短文です",
                assistant_text="了解です",
                ts=1700000000,
                dim=64,
                bits_per_dim=8,
            )
            _assert(int(wrote.get("surface", 0)) >= 1, "surface not written")
            rows = db.list_memory_items("surface", "s1", limit=5)
            _assert(len(rows) >= 1, "surface row missing")
            summary = str(rows[0]["summary"] or "")
            _assert("こ ん に ち は" not in summary, "surface summary became character-spaced")
            _assert("こんにちは" in summary, "surface summary lost user text")
        finally:
            db.close()


def main() -> None:
    test_no_char_split()
    test_strip_mem_block_keep_user_text()
    test_ingest_short_sentence_surface_ok()
    print("text_sanitization_regression: PASS")


if __name__ == "__main__":
    main()
