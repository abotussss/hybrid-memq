from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .db import MemqDB
from .quant import embed_text, f16_blob, quantize


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def import_markdown_memory(db: MemqDB, workspace_root: Path, dim: int, bits_per_dim: int) -> Dict[str, int]:
    wrote = {"surface": 0, "deep": 0}

    files: List[Path] = []
    files.extend(list((workspace_root / "memory").glob("*.md")))
    files.extend([workspace_root / "MEMORY.md", workspace_root / "IDENTITY.md", workspace_root / "SOUL.md", workspace_root / "HEARTBEAT.md"])

    for path in files:
        if not path.exists() or not path.is_file():
            continue
        text = _read_text(path).strip()
        if not text:
            continue
        session_key = "global"

        # split by paragraph and keep compact chunks
        chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
        for ch in chunks[:200]:
            summary = " ".join(ch.split())[:220]
            emb = embed_text(summary, dim)
            layer = "deep" if path.name in {"MEMORY.md", "IDENTITY.md", "SOUL.md", "HEARTBEAT.md"} else "surface"
            db.add_memory_item(
                session_key=session_key,
                layer=layer,
                text=ch[:1200],
                summary=summary,
                importance=0.68 if layer == "deep" else 0.55,
                tags={"source": str(path.name), "kind": "md_import"},
                emb_f16=f16_blob(emb),
                emb_q=quantize(emb, bits_per_dim) if layer == "deep" else None,
                emb_dim=dim,
                source="md_import",
            )
            wrote[layer] += 1

    return wrote
