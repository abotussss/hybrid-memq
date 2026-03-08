from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default


def _normalize_memctx_backend(value: str) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"", "lancedb", "memory-lancedb-pro-adapted"}:
        return "memory-lancedb-pro"
    return clean


@dataclass(frozen=True)
class Budgets:
    memctx_tokens: int
    rules_tokens: int
    style_tokens: int


@dataclass(frozen=True)
class BrainConfig:
    enabled: bool
    mode: str
    provider: str
    base_url: str
    model: str
    keep_alive: str
    timeout_ms: int
    max_tokens: int
    ingest_max_tokens: int
    recall_max_tokens: int
    merge_max_tokens: int
    audit_max_tokens: int
    concurrency: int


@dataclass(frozen=True)
class AuditConfig:
    primary_enabled: bool
    secondary_enabled: bool
    risk_threshold: float
    block_threshold: float
    allowed_languages_default: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    memctx_backend: str
    lancedb_path: Path
    lancedb_helper: Path
    host: str
    port: int
    timezone: str
    budgets: Budgets
    total_max_input_tokens: int
    total_reserve_tokens: int
    recent_max_tokens: int
    recent_min_keep_messages: int
    top_k: int
    archive_enabled: bool
    idle_enabled: bool
    idle_background_enabled: bool
    idle_seconds: int
    brain: BrainConfig
    audit: AuditConfig

    @property
    def brain_required(self) -> bool:
        return self.brain.enabled and self.brain.mode in {"required", "brain-required"}



def load_config() -> Config:
    root = Path(_env_str("MEMQ_ROOT", os.getcwd())).expanduser().resolve()
    db_raw = _env_str("MEMQ_DB_PATH", ".memq/memq_v3.sqlite3")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = (root / db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lancedb_raw = _env_str("MEMQ_LANCEDB_PATH", ".memq/lancedb")
    lancedb_path = Path(lancedb_raw)
    if not lancedb_path.is_absolute():
        lancedb_path = (root / lancedb_path).resolve()
    lancedb_path.mkdir(parents=True, exist_ok=True)
    helper_raw = _env_str(
        "MEMQ_LANCEDB_HELPER",
        str(root / "plugin" / "openclaw-memory-memq" / "src" / "memory_backend" / "lancedb_backend.mjs"),
    )
    helper_path = Path(helper_raw)
    if not helper_path.is_absolute():
        helper_path = (root / helper_path).resolve()

    return Config(
        root=root,
        db_path=db_path,
        memctx_backend=_normalize_memctx_backend(_env_str("MEMQ_MEMCTX_BACKEND", "memory-lancedb-pro")),
        lancedb_path=lancedb_path,
        lancedb_helper=helper_path,
        host=_env_str("MEMQ_HOST", "127.0.0.1"),
        port=_env_int("MEMQ_PORT", 7781),
        timezone=_env_str("MEMQ_TIMEZONE", "Asia/Tokyo"),
        budgets=Budgets(
            memctx_tokens=_env_int("MEMQ_MEMCTX_TOKENS", 500),
            rules_tokens=_env_int("MEMQ_RULES_TOKENS", 500),
            style_tokens=_env_int("MEMQ_STYLE_TOKENS", 500),
        ),
        total_max_input_tokens=_env_int("MEMQ_TOTAL_MAX_INPUT_TOKENS", 5200),
        total_reserve_tokens=_env_int("MEMQ_TOTAL_RESERVE_TOKENS", 1800),
        recent_max_tokens=_env_int("MEMQ_RECENT_TOKENS", 2600),
        recent_min_keep_messages=_env_int("MEMQ_RECENT_MIN_KEEP_MESSAGES", 4),
        top_k=_env_int("MEMQ_TOP_K", 5),
        archive_enabled=_env_bool("MEMQ_ARCHIVE_ENABLED", True),
        idle_enabled=_env_bool("MEMQ_IDLE_ENABLED", False),
        idle_background_enabled=_env_bool("MEMQ_IDLE_BACKGROUND_ENABLED", False),
        idle_seconds=_env_int("MEMQ_IDLE_SECONDS", 120),
        brain=BrainConfig(
            enabled=_env_bool("MEMQ_BRAIN_ENABLED", True),
            mode=_env_str("MEMQ_BRAIN_MODE", "brain-optional").lower(),
            provider=_env_str("MEMQ_BRAIN_PROVIDER", "ollama").lower(),
            base_url=_env_str("MEMQ_BRAIN_BASE_URL", "http://127.0.0.1:11434"),
            model=_env_str("MEMQ_BRAIN_MODEL", "gpt-oss:20b"),
            keep_alive=_env_str("MEMQ_BRAIN_KEEP_ALIVE", "30m"),
            timeout_ms=_env_int("MEMQ_BRAIN_TIMEOUT_MS", 60000),
            max_tokens=_env_int("MEMQ_BRAIN_MAX_TOKENS", 384),
            ingest_max_tokens=_env_int("MEMQ_BRAIN_INGEST_MAX_TOKENS", 384),
            recall_max_tokens=_env_int("MEMQ_BRAIN_RECALL_MAX_TOKENS", 256),
            merge_max_tokens=_env_int("MEMQ_BRAIN_MERGE_MAX_TOKENS", 128),
            audit_max_tokens=_env_int("MEMQ_BRAIN_AUDIT_MAX_TOKENS", 128),
            concurrency=max(1, _env_int("MEMQ_BRAIN_CONCURRENT", 1)),
        ),
        audit=AuditConfig(
            primary_enabled=_env_bool("MEMQ_AUDIT_PRIMARY_ENABLED", False),
            secondary_enabled=_env_bool("MEMQ_AUDIT_SECONDARY_ENABLED", False),
            risk_threshold=float(_env_str("MEMQ_AUDIT_RISK_THRESHOLD", "0.35")),
            block_threshold=float(_env_str("MEMQ_AUDIT_BLOCK_THRESHOLD", "0.85")),
            allowed_languages_default=("ja", "en"),
        ),
    )
