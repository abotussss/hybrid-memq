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
    brain_enabled: bool
    brain_provider: str
    brain_base_url: str
    brain_model: str
    brain_timeout_ms: int
    brain_keep_alive: str
    brain_temperature: float
    brain_max_tokens: int
    brain_concurrent: int
    brain_mode: str
    brain_auto_restart: bool
    brain_restart_cooldown_sec: int
    brain_restart_wait_ms: int
    brain_ingest_user_chars: int
    brain_ingest_assistant_chars: int
    brain_recall_recent_messages: int
    brain_recall_message_chars: int
    brain_merge_candidate_limit: int


def load_config() -> MemqConfig:
    root = Path(os.getenv("MEMQ_ROOT", "."))
    db_rel = os.getenv("MEMQ_DB_PATH", ".memq/sidecar.sqlite3")
    db_path = (root / db_rel).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    bits = _env_int("MEMQ_BITS_PER_DIM", 8)
    if bits not in {6, 7, 8}:
        bits = 8

    brain_mode = os.getenv("MEMQ_BRAIN_MODE", "required").strip().lower()
    if brain_mode not in {"off", "best_effort", "required"}:
        brain_mode = "required"

    return MemqConfig(
        db_path=db_path,
        dim=max(32, _env_int("MEMQ_EMBED_DIM", 256)),
        bits_per_dim=bits,
        memctx_tokens=max(32, _env_int("MEMQ_MEMCTX_TOKENS", 120)),
        rules_tokens=max(16, _env_int("MEMQ_RULES_TOKENS", 80)),
        style_tokens=max(8, _env_int("MEMQ_STYLE_TOKENS", 120)),
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
        brain_enabled=_env_bool("MEMQ_BRAIN_ENABLED", True),
        brain_provider=os.getenv("MEMQ_BRAIN_PROVIDER", "ollama"),
        brain_base_url=os.getenv("MEMQ_BRAIN_BASE_URL", "http://127.0.0.1:11434"),
        brain_model=os.getenv("MEMQ_BRAIN_MODEL", "gpt-oss:20b"),
        # Keep required-mode latency in practical range while still allowing large local models.
        brain_timeout_ms=max(500, _env_int("MEMQ_BRAIN_TIMEOUT_MS", 60000)),
        brain_keep_alive=os.getenv("MEMQ_BRAIN_KEEP_ALIVE", "30m"),
        brain_temperature=max(0.0, min(1.0, _env_float("MEMQ_BRAIN_TEMPERATURE", 0.0))),
        # Plan JSON is compact; keep generation bounded to reduce end-to-end latency.
        brain_max_tokens=max(64, _env_int("MEMQ_BRAIN_MAX_TOKENS", 256)),
        brain_concurrent=max(1, _env_int("MEMQ_BRAIN_CONCURRENT", 1)),
        brain_mode=brain_mode,
        brain_auto_restart=_env_bool("MEMQ_BRAIN_AUTO_RESTART", True),
        brain_restart_cooldown_sec=max(5, _env_int("MEMQ_BRAIN_RESTART_COOLDOWN_SEC", 30)),
        brain_restart_wait_ms=max(250, _env_int("MEMQ_BRAIN_RESTART_WAIT_MS", 2000)),
        brain_ingest_user_chars=max(120, _env_int("MEMQ_BRAIN_INGEST_USER_CHARS", 2400)),
        brain_ingest_assistant_chars=max(120, _env_int("MEMQ_BRAIN_INGEST_ASSISTANT_CHARS", 1200)),
        brain_recall_recent_messages=max(1, _env_int("MEMQ_BRAIN_RECALL_RECENT_MESSAGES", 4)),
        brain_recall_message_chars=max(80, _env_int("MEMQ_BRAIN_RECALL_MESSAGE_CHARS", 220)),
        brain_merge_candidate_limit=max(20, _env_int("MEMQ_BRAIN_MERGE_CANDIDATE_LIMIT", 80)),
    )
