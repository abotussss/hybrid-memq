from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any


@dataclass(frozen=True)
class LanceDbMemoryBackend:
    db_path: Path
    helper_path: Path

    def enabled(self) -> bool:
        return self.helper_path.exists()

    def _run(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError(f"lancedb helper not found: {self.helper_path}")
        proc = subprocess.run(
            ["node", str(self.helper_path), command],
            input=json.dumps({"dbPath": str(self.db_path), **payload}, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"lancedb helper failed: {command}")
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid lancedb helper output: {exc}") from exc
        if not data.get("ok", False):
            raise RuntimeError(str(data.get("error") or f"lancedb helper returned not ok for {command}"))
        return data

    def ingest_memories(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        self._run("ingest", {"entries": entries})

    def search_memories(
        self,
        *,
        session_key: str,
        queries: list[str],
        fact_keys: list[str],
        layer: str,
        limit: int,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        data = self._run(
            "query",
            {
                "sessionKey": session_key,
                "queries": queries,
                "factKeys": fact_keys,
                "layer": layer,
                "limit": limit,
                "includeGlobal": include_global,
            },
        )
        return list(data.get("items") or [])

    def list_entries(
        self,
        *,
        session_key: str,
        kinds: list[str] | None = None,
        include_global: bool = True,
        limit: int = 50,
        layer: str = "",
        fact_key_prefixes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        data = self._run(
            "list",
            {
                "sessionKey": session_key,
                "kinds": kinds or [],
                "includeGlobal": include_global,
                "limit": limit,
                "layer": layer,
                "factKeyPrefixes": fact_key_prefixes or [],
            },
        )
        return list(data.get("items") or [])
