# sidecar

Local vector + quant engine for memq.

## Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn memq_sidecar.app:app --host 127.0.0.1 --port 7781
```

## Endpoints
- `GET /health`
- `GET /stats`
- `POST /embed`
- `POST /index/add`
- `POST /index/search`
- `POST /index/touch`
- `POST /index/consolidate`
- `POST /index/rebuild`

## Storage
- SQLite DB: `.memq/sidecar.sqlite3`
- Embedding code: scalar int8 quantized bytes

## Notes
- ANNは現在 brute-force cosine（MVP）
- FAISS/PQ backend への置換ポイント: `/index/search` と `/index/rebuild`
