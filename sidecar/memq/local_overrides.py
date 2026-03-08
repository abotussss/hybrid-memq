from __future__ import annotations

from pathlib import Path
import json


def qstyle_current_path(root: Path) -> Path:
    return root / "QSTYLE.current.json"


def qrule_current_path(root: Path) -> Path:
    return root / "QRULE.current.json"


def qctx_current_path(root: Path) -> Path:
    return root / "QCTX.current.txt"


def write_current_snapshots(root: Path, *, qstyle: dict[str, str] | None = None, qrule: dict[str, str] | None = None, qctx: str | None = None) -> None:
    if qstyle is not None:
        qstyle_current_path(root).write_text(json.dumps(qstyle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if qrule is not None:
        qrule_current_path(root).write_text(json.dumps(qrule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if qctx is not None:
        qctx_current_path(root).write_text(str(qctx or ""), encoding="utf-8")
