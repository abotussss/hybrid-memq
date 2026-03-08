from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re


QSTYLE_ALLOWED_KEYS = {
    "tone",
    "persona",
    "verbosity",
    "speaking_style",
    "callUser",
    "firstPerson",
    "prefix",
}

QRULE_ALLOWED_PREFIXES = (
    "security.",
    "language.",
    "procedure.",
    "compliance.",
    "output.",
    "operation.",
)

BLOCK_MARKERS = ("<mem", "<qrule", "<qstyle", "<qctx", "budget_tokens=")


@dataclass(frozen=True)
class LocalOverrides:
    qstyle: dict[str, str]
    qrule: dict[str, str]
    qstyle_path: Path
    qrule_path: Path


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


def _normalize(value: str) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def _parse_mapping(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            data[key.strip()] = value.strip()
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        out[str(key).strip()] = _normalize(str(value))
    return out


def _style_override_is_dirty(key: str, value: str) -> bool:
    if key not in QSTYLE_ALLOWED_KEYS:
        return True
    lowered = value.lower()
    if not value:
        return True
    return any(marker in lowered for marker in BLOCK_MARKERS)


def _rule_override_is_dirty(key: str, value: str) -> bool:
    if not any(key.startswith(prefix) for prefix in QRULE_ALLOWED_PREFIXES):
        return True
    lowered = value.lower()
    if not value:
        return True
    return any(marker in lowered for marker in BLOCK_MARKERS)


def load_local_overrides(root: Path) -> LocalOverrides:
    qstyle_root = root / "QSTYLE.local.json"
    qrule_root = root / "QRULE.local.json"
    qstyle_path = qstyle_root
    qrule_path = qrule_root

    raw_qstyle = _parse_mapping(qstyle_path)
    raw_qrule = _parse_mapping(qrule_path)

    qstyle = {key: value for key, value in raw_qstyle.items() if not _style_override_is_dirty(key, value)}
    qrule = {key: value for key, value in raw_qrule.items() if not _rule_override_is_dirty(key, value)}

    return LocalOverrides(
        qstyle=qstyle,
        qrule=qrule,
        qstyle_path=qstyle_path,
        qrule_path=qrule_path,
    )
