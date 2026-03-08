# OpenClaw Setup

## English

```bash
cd /Users/hiroyukimiyake/Documents/New\ project
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
cd /Users/hiroyukimiyake/Documents/New\ project
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

期待値:

- OpenClaw の memory plugin は `openclaw-memory-memq`
- sidecar health は `ok=true`
- `qctxBackend = memory-lancedb-pro`
