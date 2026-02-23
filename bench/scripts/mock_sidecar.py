#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 256
STORE = {}


def embed_text(text: str):
    h = hashlib.sha256(text.encode("utf-8")).digest()
    arr = [(h[i % len(h)] - 127.5) / 127.5 for i in range(DIM)]
    n = math.sqrt(sum(x * x for x in arr)) or 1.0
    return [x / n for x in arr]


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _read(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if self.path == "/health":
            return self._json({"ok": True, "size": len(STORE), "dim": DIM})
        if self.path == "/stats":
            return self._json({"size": len(STORE)})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read()
        if self.path == "/embed":
            return self._json({"vector": embed_text(body.get("text", ""))})
        if self.path == "/index/add":
            STORE[body["id"]] = body
            return self._json({"ok": True})
        if self.path == "/index/search":
            q = body.get("vector", [0.0] * DIM)
            k = int(body.get("k", 5))
            scored = []
            for it in STORE.values():
                v = it.get("vector", [0.0] * DIM)
                scored.append((cos(q, v), it))
            scored.sort(key=lambda x: x[0], reverse=True)
            items = []
            for s, it in scored[:k]:
                items.append(
                    {
                        "id": it["id"],
                        "score": s,
                        "type": it.get("type", "note"),
                        "confidence": it.get("confidence", 0.7),
                        "importance": it.get("importance", 0.5),
                        "facts": it.get("facts", []),
                        "rawText": it.get("rawText", ""),
                    }
                )
            return self._json({"items": items})
        if self.path in ("/index/touch", "/index/consolidate", "/index/rebuild"):
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def main():
    s = HTTPServer(("127.0.0.1", 17781), H)
    print("mock sidecar on :17781")
    s.serve_forever()


if __name__ == "__main__":
    main()
