# OpenClaw setup

## Install plugin (dev link)
```bash
openclaw plugins install -l /Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq
```

## Configure memory slot
`plugins.slots.memory = "openclaw-memory-memq"`

## Recommended memq config
- `memq.budgetTokens`: e.g. `120`
- `memq.topK`: e.g. `5`
- `memq.surface.max`: e.g. `120`
- `memq.writeGate.low/high`
- `memq.fallback.maxScoreMin/entropyMax`

## Start sidecar
```bash
cd /Users/hiroyukimiyake/Documents/New project/sidecar
source .venv/bin/activate
uvicorn memq_sidecar.app:app --host 127.0.0.1 --port 7781
```
