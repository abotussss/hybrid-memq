#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/hiroyukimiyake/Documents/New project"
cd "$ROOT"

python3 bench/scripts/mock_sidecar.py > /tmp/memq_mock_sidecar.log 2>&1 &
SIDE_PID=$!
python3 bench/scripts/mock_openai.py > /tmp/memq_mock_openai.log 2>&1 &
OPENAI_PID=$!
cleanup() {
  kill "$SIDE_PID" "$OPENAI_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sleep 0.4

python3 - <<'PY'
import json, urllib.request, hashlib

def post(url, body):
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type":"application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

base = "http://127.0.0.1:17781"
for i in range(8):
    raw = (
        "Project memory chunk. " * 60
        + f" chunk={i} goal=token_minimize tone=keigo avoid=extra_suggestions "
    )
    emb = post(f"{base}/embed", {"text": raw})["vector"]
    post(
        f"{base}/index/add",
        {
            "id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24],
            "vector": emb,
            "tsSec": 1,
            "type": "preference" if i % 2 == 0 else "note",
            "importance": 0.8,
            "confidence": 0.9,
            "strength": 0.7,
            "volatilityClass": "low",
            "facts": [
                {"k":"tone","v":"keigo","conf":0.95},
                {"k":"goal","v":"token_minimize","conf":0.9},
                {"k":"avoidance_rules","v":"no_extra_suggestions","conf":0.9},
            ],
            "tags": ["fixture","memq"],
            "rawText": raw,
        },
    )
print("seeded")
PY

python3 bench/scripts/replay_api_mode.py \
  --dataset bench/data/replay.jsonl \
  --output bench/results/api_replay_smoke.csv \
  --sidecar http://127.0.0.1:17781 \
  --base-url http://127.0.0.1:18000/v1 \
  --api-key dummy \
  --model mock-model

echo "smoke done: bench/results/api_replay_smoke.csv"
