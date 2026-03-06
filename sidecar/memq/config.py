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

    return Config(
        root=root,
        db_path=db_path,
        host=_env_str("MEMQ_HOST", "127.0.0.1"),
        port=_env_int("MEMQ_PORT", 7781),
        timezone=_env_str("MEMQ_TIMEZONE", "Asia/Tokyo"),
        budgets=Budgets(
            memctx_tokens=_env_int("MEMQ_MEMCTX_TOKENS", 120),
            rules_tokens=_env_int("MEMQ_RULES_TOKENS", 80),
            style_tokens=_env_int("MEMQ_STYLE_TOKENS", 120),
        ),
        total_max_input_tokens=_env_int("MEMQ_TOTAL_MAX_INPUT_TOKENS", 4200),
        total_reserve_tokens=_env_int("MEMQ_TOTAL_RESERVE_TOKENS", 1800),
        recent_max_tokens=_env_int("MEMQ_RECENT_TOKENS", 2600),
        recent_min_keep_messages=_env_int("MEMQ_RECENT_MIN_KEEP_MESSAGES", 4),
        top_k=_env_int("MEMQ_TOP_K", 5),
        archive_enabled=_env_bool("MEMQ_ARCHIVE_ENABLED", True),
        idle_enabled=_env_bool("MEMQ_IDLE_ENABLED", True),
        idle_seconds=_env_int("MEMQ_IDLE_SECONDS", 120),
        brain=BrainConfig(
            enabled=_env_bool("MEMQ_BRAIN_ENABLED", True),
            mode=_env_str("MEMQ_BRAIN_MODE", "brain-optional").lower(),
            provider=_env_str("MEMQ_BRAIN_PROVIDER", "ollama").lower(),
            base_url=_env_str("MEMQ_BRAIN_BASE_URL", "http://127.0.0.1:11434"),
            model=_env_str("MEMQ_BRAIN_MODEL", "gpt-oss:20b"),
            keep_alive=_env_str("MEMQ_BRAIN_KEEP_ALIVE", "30m"),
            timeout_ms=_env_int("MEMQ_BRAIN_TIMEOUT_MS", 60000),
            max_tokens=_env_int("MEMQ_BRAIN_MAX_TOKENS", 640),
            concurrency=max(1, _env_int("MEMQ_BRAIN_CONCURRENT", 1)),
        ),
        audit=AuditConfig(
            primary_enabled=_env_bool("MEMQ_AUDIT_PRIMARY_ENABLED", True),
            secondary_enabled=_env_bool("MEMQ_AUDIT_SECONDARY_ENABLED", False),
            risk_threshold=float(_env_str("MEMQ_AUDIT_RISK_THRESHOLD", "0.35")),
            block_threshold=float(_env_str("MEMQ_AUDIT_BLOCK_THRESHOLD", "0.85")),
            allowed_languages_default=("ja", "en"),
        ),
    )
