# OpenClaw Setup

## English

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

Expected:

- OpenClaw memory plugin = `openclaw-memory-memq`
- sidecar health = `ok=true`
- `qctxBackend = memory-lancedb-pro`

## 日本語

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

期待値:

- OpenClaw の memory plugin は `openclaw-memory-memq`
- sidecar health は `ok=true`
- `qctxBackend = memory-lancedb-pro`
