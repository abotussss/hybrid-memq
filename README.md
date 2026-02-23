# hybrid-memq

A production-focused memory plugin for OpenClaw with a **Surface / Deep / Ephemeral** memory model and fixed-budget **MEMCTX** injection.

`hybrid-memq` replaces the default memory slot with a compact retrieval-and-injection pipeline designed to improve long-session memory quality while reducing input-token cost.

## Features
- Surface / Deep / Ephemeral memory architecture
- Fixed-budget MEMCTX compilation (`k=v` fact DSL)
- OpenClaw hook integration (`before_prompt_build`, `agent_end`, `before_compaction`, `gateway_start`)
- Local sidecar (SQLite + embedding/retrieval + consolidation + audit)
- Preference/profile learning (non-LLM, local rules + decay aggregation)
- Memory quarantine for suspicious/polluting facts
- Optional high-risk dual output audit (rule-based + secondary LLM audit)
- Seamless enable/disable switch for OpenClaw memory slot

## Repository Layout
```text
core/                         Shared memory logic (scoring, memctx, gates, decay)
plugin/openclaw-memory-memq/  OpenClaw memory plugin (TypeScript)
sidecar/                      Local sidecar (Python)
docs/                         Design and operations docs
examples/                     Example OpenClaw config
scripts/                      One-command setup/switch helpers
memq.yaml                     Reference configuration
```

## Requirements
- OpenClaw installed locally
- Node.js 20+ and pnpm
- Python 3.10+

## Quick Start
### 1) Build plugin
```bash
cd ~/hybrid-memq/plugin/openclaw-memory-memq
pnpm install
pnpm build
```

### 2) Install plugin into OpenClaw
```bash
openclaw plugins install -l ~/hybrid-memq/plugin/openclaw-memory-memq
```

### 3) Start sidecar
Minimal mode (no extra deps):
```bash
cd ~/hybrid-memq/sidecar
python3 minisidecar.py
```

Or FastAPI mode:
```bash
cd ~/hybrid-memq/sidecar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn memq_sidecar.app:app --host 127.0.0.1 --port 7781
```

### 4) Enable hybrid-memq in OpenClaw
```bash
scripts/memq-openclaw.sh quickstart
```

### 5) Verify runtime
```bash
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

## CLI Commands
`scripts/memq-openclaw.sh`

| Command | Purpose |
|---|---|
| `install` | Install/link plugin into OpenClaw |
| `enable` | Enable `openclaw-memory-memq` memory slot (backup existing config) |
| `disable` | Restore previous OpenClaw config from backup |
| `on` | Shortcut for `quickstart` |
| `off` | Disable MEMQ and stop sidecar |
| `start-sidecar` | Start local sidecar |
| `stop-sidecar` | Stop local sidecar |
| `status` | Show plugin/slot/sidecar status |
| `quickstart` | `install + start-sidecar + enable + status` |
| `audit-on <url> <model> [risk_threshold] [block_threshold]` | Enable secondary LLM audit for high-risk outputs |
| `audit-off` | Disable secondary LLM audit (MEMQ itself stays enabled) |
| `audit-status` | Show current audit env values |

## How It Works
### Runtime (per turn)
1. Build query embedding from current user turn.
2. Retrieve from Surface first.
3. Retrieve from Deep only when needed.
4. Re-rank candidates and compile MEMCTX facts under strict token budget.
5. Inject MEMCTX into OpenClaw prompt context.
6. Update access stats and refresh Surface after response.

### Sleep Consolidation (idle)
The sidecar monitors activity and runs consolidation when idle:
- strength decay
- low-value pruning
- dedup/merge
- conflict refresh
- preference/profile refresh
- reindex (when needed)

No API LLM call is required for this idle consolidation loop.

## MEMCTX and MEMRULES
- **MEMCTX**: compact recall context (memory facts), fixed token budget.
- **MEMRULES**: strict rule channel (separate budget) for non-negotiable constraints.

Both are budgeted to avoid prompt growth outliers.

## Configuration
Main knobs (OpenClaw plugin config):
- `memq.sidecarUrl` (default `http://127.0.0.1:7781`)
- `memq.budgetTokens` (default `120`)
- `memq.topK` (default `5`)
- `memq.surface.max` (default `120`)
- `memq.rules.budgetTokens` (default `80`)
- `memq.rules.strict` (default `false`)
- `memq.rules.allowedLanguages` (default empty)
- `memq.rules.hard` (default empty, `|`-separated)

Reference: `memq.yaml`

## OpenClaw Integration
Example config: `examples/openclaw.json`

Key points:
- plugin is loaded via `plugins.load.paths`
- memory slot is switched via `plugins.slots.memory = "openclaw-memory-memq"`

Rollback is one command:
```bash
scripts/memq-openclaw.sh disable
```

## Security Model
- Secrets are never stored in MEMCTX.
- Suspicious memory facts are quarantined and excluded from recall output.
- High-risk output can trigger secondary LLM audit (optional).

## Documentation
- Setup: `docs/openclaw-setup.md`
- Architecture: `docs/architecture.md`
- Security: `docs/security.md`

## License
MIT (`LICENSE`)
