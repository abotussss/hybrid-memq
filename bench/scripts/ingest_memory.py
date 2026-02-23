#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List


def post_json(base: str, path: str, payload: Dict) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{path}",
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def chunk_markdown(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[str] = []
    for p in parts:
        if len(p) <= 500:
            out.append(p)
        else:
            for i in range(0, len(p), 420):
                out.append(p[i : i + 420])
    return out


def extract_facts(chunk: str) -> List[Dict]:
    lines = [re.sub(r"^[-*]\s+", "", x).strip() for x in chunk.splitlines() if x.strip()]
    facts = []
    for line in lines[:6]:
        m = re.match(r"^([^:]{1,28})[:：]\s*(.{1,140})$", line)
        if m:
            facts.append({"k": m.group(1).lower().replace(" ", "_"), "v": m.group(2), "conf": 0.82})
        elif len(line) <= 80:
            facts.append({"k": "note", "v": line, "conf": 0.65})
    return facts


def infer_type(chunk: str) -> str:
    s = chunk.lower()
    if re.search(r"(must|don't|do not|禁止|必須)", s):
        return "constraint"
    if re.search(r"(prefer|like|口調|好み)", s):
        return "preference"
    if re.search(r"(i am|my name|私は)", s):
        return "identity"
    if re.search(r"(plan|todo|deadline|期限)", s):
        return "plan"
    return "note"


def volatility_for(t: str) -> str:
    if t in ("constraint", "identity"):
        return "low"
    if t in ("preference", "plan"):
        return "medium"
    return "high"


def collect_files(workspace: Path) -> List[Path]:
    files = []
    memory_md = workspace / "MEMORY.md"
    if memory_md.exists():
        files.append(memory_md)
    memory_dir = workspace / "memory"
    if memory_dir.exists():
        files.extend(sorted(memory_dir.glob("*.md")))
    return files


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", default=".")
    p.add_argument("--sidecar", default="http://127.0.0.1:7781")
    args = p.parse_args()

    ws = Path(args.workspace).resolve()
    files = collect_files(ws)
    if not files:
        print("no memory files found (MEMORY.md or memory/*.md)")
        return 0

    added = 0
    ts = int(time.time())
    for f in files:
        text = f.read_text(encoding="utf-8")
        for i, chunk in enumerate(chunk_markdown(text)):
            emb = post_json(args.sidecar, "/embed", {"text": chunk})
            vec = emb["vector"]
            t = infer_type(chunk)
            payload = {
                "id": hashlib.sha256(f"{f}:{i}:{chunk}".encode("utf-8")).hexdigest()[:24],
                "vector": vec,
                "tsSec": ts,
                "type": t,
                "importance": 0.9 if re.search(r"(remember|覚えて)", chunk, re.I) else 0.55,
                "confidence": 0.72,
                "strength": 0.55,
                "volatilityClass": volatility_for(t),
                "facts": extract_facts(chunk),
                "tags": ["memory_md", t],
                "evidenceUri": str(f),
                "rawText": chunk,
            }
            post_json(args.sidecar, "/index/add", payload)
            added += 1

    print(json.dumps({"ok": True, "added": added, "files": [str(f) for f in files]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
