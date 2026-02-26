# MEMQ Sidecar

Local FastAPI service for Hybrid MEMQ v2 (Mode A).

## Run

```bash
cd /path/to/hybrid-memq
python3 -m venv sidecar/.venv
sidecar/.venv/bin/pip install -r sidecar/requirements.txt
sidecar/.venv/bin/python sidecar/minisidecar.py
```

## Health

```bash
curl -sS http://127.0.0.1:7781/health
```

## Main endpoints

- `POST /memctx/query`
- `POST /memory/ingest_turn`
- `POST /conversation/summarize`
- `POST /idle/run_once`
- `POST /audit/output`
- `GET /profile`
- `GET /quarantine`
