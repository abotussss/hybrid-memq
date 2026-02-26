from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


@dataclass(frozen=True)
class MemqConfig:
    db_path: Path
    dim: int
    bits_per_dim: int
    memctx_tokens: int
    rules_tokens: int
    style_tokens: int
    recent_tokens: int
    retrieval_top_k: int
    surface_threshold: float
    deep_enabled: bool
    idle_enabled: bool
    idle_seconds: int
    llm_audit_enabled: bool
    llm_audit_url: str
    llm_audit_model: str
    llm_audit_api_key: str
    llm_audit_timeout_sec: float


def load_config() -> MemqConfig:
    root = Path(os.getenv("MEMQ_ROOT", "."))
    db_rel = os.getenv("MEMQ_DB_PATH", ".memq/sidecar.sqlite3")
    db_path = (root / db_rel).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    bits = _env_int("MEMQ_BITS_PER_DIM", 8)
    if bits not in {6, 7, 8}:
        bits = 8

    return MemqConfig(
        db_path=db_path,
        dim=max(32, _env_int("MEMQ_EMBED_DIM", 256)),
        bits_per_dim=bits,
        memctx_tokens=max(32, _env_int("MEMQ_MEMCTX_TOKENS", 120)),
        rules_tokens=max(16, _env_int("MEMQ_RULES_TOKENS", 80)),
        style_tokens=max(8, _env_int("MEMQ_STYLE_TOKENS", 24)),
        recent_tokens=max(400, _env_int("MEMQ_RECENT_TOKENS", 5000)),
        retrieval_top_k=max(1, _env_int("MEMQ_TOP_K", 5)),
        surface_threshold=max(0.0, min(1.0, _env_float("MEMQ_SURFACE_THRESHOLD", 0.85))),
        deep_enabled=_env_bool("MEMQ_DEEP_ENABLED", True),
        idle_enabled=_env_bool("MEMQ_IDLE_ENABLED", True),
        idle_seconds=max(15, _env_int("MEMQ_IDLE_SECONDS", 120)),
        llm_audit_enabled=_env_bool("MEMQ_LLM_AUDIT_ENABLED", False),
        llm_audit_url=os.getenv("MEMQ_LLM_AUDIT_URL", "https://api.openai.com/v1/chat/completions"),
        llm_audit_model=os.getenv("MEMQ_LLM_AUDIT_MODEL", "gpt-5.2"),
        llm_audit_api_key=os.getenv("MEMQ_LLM_AUDIT_API_KEY", ""),
        llm_audit_timeout_sec=max(2.0, _env_float("MEMQ_LLM_AUDIT_TIMEOUT_SEC", 20.0)),
    )
