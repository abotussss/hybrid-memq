# Hybrid MEMQ v3 Sidecar

The sidecar is the single source of truth for Hybrid MEMQ v3.

Responsibilities:
- persist memory state in SQLite
- call QBRAIN for ingest / recall / merge planning
- apply those plans deterministically
- expose proof endpoints for required runtime
- provide bounded `QRULE`, `QSTYLE`, `QCTX` to the plugin

## Run

```bash
cd /Users/hiroyukimiyake/Documents/New\ project
python3 -m venv sidecar/.venv
sidecar/.venv/bin/pip install -r sidecar/requirements.txt
MEMQ_BRAIN_MODE=brain-required \
MEMQ_BRAIN_MODEL=gpt-oss:20b \
sidecar/.venv/bin/python sidecar/minisidecar.py
```

## Important Endpoints

- `GET /health`
- `POST /memory/ingest_turn`
- `POST /memctx/query`
- `POST /idle/run_once`
- `POST /audit/output`
- `GET /profile`
- `GET /quarantine`
- `GET /brain/stats`
- `GET /brain/trace/recent`
