from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from sidecar.memq.config import BrainConfig


class BrainUnavailable(RuntimeError):
    pass


@dataclass
class OllamaResult:
    content: dict[str, Any]
    stats: dict[str, Any]
    ps_snapshot: dict[str, Any] | None
    prompt_sha256: str


class OllamaClient:
    def __init__(self, cfg: BrainConfig) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), timeout=cfg.timeout_ms / 1000.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _think_mode(self) -> Any:
        model = self.cfg.model.lower()
        if model.startswith("gpt-oss") or "gpt-oss" in model:
            return "low"
        if model.startswith("qwen") or "qwen" in model:
            return False
        return False

    async def _ps_snapshot(self) -> dict[str, Any] | None:
        try:
            res = await self._client.get("/api/ps")
            res.raise_for_status()
            payload = res.json()
        except Exception as exc:  # noqa: BLE001
            raise BrainUnavailable(f"ollama_ps_failed:{type(exc).__name__}") from exc
        models = payload.get("models") or []
        for model in models:
            name = str(model.get("model") or model.get("name") or "")
            if name == self.cfg.model:
                return model
        return None

    async def chat_schema(self, *, system: str, user: str, schema_model: type[BaseModel]) -> OllamaResult:
        schema = schema_model.model_json_schema()
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "format": schema,
            "think": self._think_mode(),
            "options": {
                "temperature": 0,
                "num_predict": self.cfg.max_tokens,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        prompt_sha256 = hashlib.sha256(user.encode("utf-8", "ignore")).hexdigest()
        try:
            res = await self._client.post("/api/chat", json=payload)
            res.raise_for_status()
            body = res.json()
            content_raw = ((body.get("message") or {}).get("content") or "").strip()
            if not content_raw:
                raise BrainUnavailable("brain_empty_content")
            try:
                parsed = json.loads(content_raw)
            except json.JSONDecodeError as exc:
                raise BrainUnavailable("brain_invalid_json") from exc
            validated = schema_model.model_validate(parsed)
            ps = await self._ps_snapshot()
            if ps is None:
                raise BrainUnavailable("brain_proof_failed")
            stats = {
                "prompt_eval_count": body.get("prompt_eval_count"),
                "eval_count": body.get("eval_count"),
                "total_duration": body.get("total_duration"),
                "load_duration": body.get("load_duration"),
            }
            return OllamaResult(content=validated.model_dump(), stats=stats, ps_snapshot=ps, prompt_sha256=prompt_sha256)
        except ValidationError as exc:
            raise BrainUnavailable(f"brain_schema_validation_failed:{exc.errors()[:1]}") from exc
        except httpx.TimeoutException as exc:
            raise BrainUnavailable("brain_timeout") from exc
        except httpx.HTTPError as exc:
            raise BrainUnavailable(f"brain_http_error:{type(exc).__name__}") from exc


def load_prompt(path: Path, fallback: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        return text.strip() or fallback.strip()
    except FileNotFoundError:
        return fallback.strip()
