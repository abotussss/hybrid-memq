# OpenClaw Setup (Mode A)

## 1) Quick setup

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
```

This runs plugin build/install, sidecar startup, and memory slot switch to `openclaw-memory-memq`.

## 2) Verify

```bash
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

Expected:

- memory slot = `openclaw-memory-memq`
- sidecar `ok=true`

## 3) Optional: secondary LLM audit

```bash
export MEMQ_LLM_AUDIT_API_KEY='YOUR_API_KEY'
scripts/memq-openclaw.sh audit-on https://api.openai.com/v1/chat/completions gpt-5.2 0.20 0.85
```

Disable:

```bash
scripts/memq-openclaw.sh audit-off
```

## 4) Restore previous backend

```bash
scripts/memq-openclaw.sh disable
```

This restores the pre-MEMQ plugin slot/config saved during `enable`.
