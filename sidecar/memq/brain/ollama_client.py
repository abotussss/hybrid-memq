from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
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

    @staticmethod
    def _extract_json_text(body: dict[str, Any]) -> str:
        message = body.get("message") or {}
        content = str(message.get("content") or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            content = content.strip()
        if not content:
            return ""
        if "<think>" in content:
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if not content:
            return ""
        return content

    @staticmethod
    def _extract_balanced_object(text: str) -> str:
        start = text.find("{")
        if start < 0:
            return text
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return text[start:]

    @classmethod
    def _repair_json_text(cls, text: str) -> str:
        candidate = cls._extract_balanced_object(text.strip())
        candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)
        return candidate.strip()

    async def _chat_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        res = await self._client.post("/api/chat", json=payload)
        res.raise_for_status()
        return res.json()

    async def _repair_with_model(self, *, broken_text: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        repair_payload = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "format": schema,
            "think": False,
            "options": {
                "temperature": 0,
                "num_predict": min(max_tokens, 256),
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return exactly one JSON object matching the provided schema. "
                        "You are repairing a previous malformed JSON candidate. "
                        "Do not explain. Do not add markdown."
                    ),
                },
                {"role": "user", "content": broken_text},
            ],
        }
        return await self._chat_once(repair_payload)

    async def chat_schema(self, *, system: str, user: str, schema_model: type[BaseModel], max_tokens: int | None = None) -> OllamaResult:
        schema = schema_model.model_json_schema()
        token_cap = int(max_tokens or self.cfg.max_tokens)
        payload = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "format": schema,
            "think": self._think_mode(),
            "options": {
                "temperature": 0,
                "num_predict": token_cap,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        prompt_sha256 = hashlib.sha256(user.encode("utf-8", "ignore")).hexdigest()
        try:
            body = await self._chat_once(payload)
            content_raw = self._extract_json_text(body)
            if not content_raw:
                retry_payload = dict(payload)
                retry_payload["think"] = False
                retry_payload["options"] = dict(payload.get("options") or {})
                retry_payload["options"]["num_predict"] = min(int(retry_payload["options"].get("num_predict") or self.cfg.max_tokens), 256)
                retry_payload["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            system.strip()
                            + "\n\n"
                            + "Return exactly one JSON object matching the schema. "
                            + "No prose. No markdown. No thinking text."
                        ),
                    },
                    {"role": "user", "content": user},
                ]
                body = await self._chat_once(retry_payload)
                content_raw = self._extract_json_text(body)
            if not content_raw:
                raise BrainUnavailable("brain_empty_content")
            try:
                parsed = json.loads(content_raw)
            except json.JSONDecodeError as exc:
                repaired = self._repair_json_text(content_raw)
                try:
                    parsed = json.loads(repaired)
                except json.JSONDecodeError:
                    try:
                        repaired_body = await self._repair_with_model(
                            broken_text=content_raw,
                            schema=schema,
                            max_tokens=token_cap,
                        )
                        repaired_raw = self._extract_json_text(repaired_body)
                        repaired_text = self._repair_json_text(repaired_raw)
                        parsed = json.loads(repaired_text)
                        body = repaired_body
                    except Exception:
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
