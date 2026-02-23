#!/usr/bin/env python3
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def tok(s: str) -> int:
    return max(1, len(s) // 4)


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        messages = body.get("messages", [])
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        prompt_tokens = tok(prompt)
        answer = "了解しました。条件を維持して回答します。"
        completion_tokens = tok(answer)
        return self._json(
            {
                "id": "mockcmpl-1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )


def main():
    s = HTTPServer(("127.0.0.1", 18000), H)
    print("mock openai on :18000")
    s.serve_forever()


if __name__ == "__main__":
    main()
