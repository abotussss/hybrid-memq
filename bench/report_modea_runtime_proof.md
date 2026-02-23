# Mode A Runtime Proof and Hardening Verification

Date: 2026-02-23
Workspace: `/Users/hiroyukimiyake/Documents/New project`

## 1) Proof that OpenClaw is using MEMQ plugin hooks

### OpenClaw runtime output (actual run)
Observed during:
- `openclaw agent --local --agent main --message "runtime_single_hook_check" --json`

Key plugin evidence from stdout:
- `[plugins] [memq] before_prompt_build session=agent:main:main mode=api_text ... injected_tokens=116`
- `[plugins] [memq] metrics turns=1 ...`

This proves the plugin hook is active in OpenClaw runtime and MEMCTX compilation is executed per turn.

## 2) Proof that OpenClaw is calling sidecar endpoints

### Sidecar request logs (same runtime window)
Observed from running `python3 sidecar/minisidecar.py` in foreground:
- `POST /idle_tick`
- `POST /embed`
- `GET /profile`
- `POST /index/search`

Earlier preference-focused run additionally showed:
- `POST /preference/event`

These endpoint calls are emitted only if plugin->sidecar integration is live.

## 3) Proof that memory is stored/retrieved with safeguards

### Hardening test execution
- Command: `python3 bench/scripts/test_modea_hardening.py`
- Result: `ok: true`

Validated by script:
1. Pollution input is quarantined (`memory_quarantine`)
2. Preference/profile learning is updated (`preference_profile`, `memory_policy_profile`)
3. Consolidation runs (decay/prune/merge/conflict refresh)

Sample result snapshot:
- `quarantineSize: 3`
- `conflictGroups: 1`
- `profile.pref_keys: 4`
- `profile.policy_keys: 2`

## 4) Budget enforcement and production-like benchmark

### MEMCTX budget
- MEMCTX compiler enforces line-by-line token budget in:
  - `plugin/openclaw-memory-memq/src/memq_core.ts`

### Outlier detection test
- Script: `bench/scripts/check_memctx_budget.py`
- Run: `python3 bench/scripts/check_memctx_budget.py --csv bench/results/prod_like_detail_5_after_hooks.csv --budget 120`
- Output: `{'rows': 10, 'bad_budget': 0, 'bad_hybrid_input': 0}`
- Exit code: `0`

Interpretation:
- MEMCTX budget did not break (`bad_budget=0`)
- No hybrid provider-input outlier was observed in this rerun (`bad_hybrid_input=0`)

### Production-like summary (N=5)
From `bench/results/prod_like_summary_5_after_hooks.csv`:
- `baseline_full`:
  - `accuracy=1.0`
  - `avg_input_tokens=3813`, `p95_input_tokens=3869`
  - `avg_duration_ms=4015.6`, `p95_duration_ms=5585.0`
- `hybrid_memctx`:
  - `accuracy=1.0`
  - `avg_input_tokens=598`, `p95_input_tokens=642`
  - `avg_memctx_tokens_est=117` (under 120 budget)
  - `avg_duration_ms=3062.4`, `p95_duration_ms=3324.0`

Relative gain (`hybrid_memctx` vs `baseline_full`):
- input tokens: `-84.3%` (3813 -> 598)
- p95 input tokens: `-83.4%` (3869 -> 642)
- avg latency: `-23.7%` (4015.6ms -> 3062.4ms)
- p95 latency: `-40.5%` (5585.0ms -> 3324.0ms)
- accuracy: no drop on this run (`1.0 -> 1.0`)

## 5) MEMRULES strict output-audit verification

### Runtime proof
Observed in OpenClaw runtime output:
- `[plugins] [memq] before_prompt_build ... rules_tokens=76 injected_tokens=116`
- `[plugins] [memq] agent_end refs=0 audited=2 violations=2`

This proves:
- MEMRULES is injected with separate budget (`rules_tokens`)
- output audit path is executed each turn (`audited=...`)

### Sidecar evidence
During the same run, sidecar logs contained:
- `POST /audit/output` (multiple calls)

Current counters:
- `GET /audit/stats` -> `{"count": 8, "violations": 6, "avgRisk": 0.15, "passRate": 0.25}`
- `GET /stats` includes:
  - `outputAuditCount`
  - `outputAuditViolations`

### Positive/negative probes
- clean Japanese output probe:
  - request: `allowedLanguages=["ja"]`, Japanese-only text
  - result: `passed=true`, `riskScore=0.0`
- malicious probe:
  - text included: `ignore previous instructions ... api key sk-...`
  - result: `passed=false`, `riskScore=1.0`, reasons:
    - `openai_key_pattern`
    - `secret_term_output`
    - `mentions_prompt_override_terms`

## 6) Implemented additions for requested Mode A hardening

### Data model extensions (SQLite)
Implemented in sidecar:
- `preference_profile`
- `preference_event`
- `memory_policy_profile`
- `memory_quarantine`
- `conflict_group`
- `capsule` (MVP type-based)

### Learning (local, no API LLM call)
- Exponential-decay aggregation from `preference_event`
- Profile refresh updates:
  - `preference_profile`
  - `memory_policy_profile`

### Pollution defense
- Fact schema whitelist + sanitizer
- Prompt-injection-like pattern screening
- Quarantine path for rejected/unsafe content
- Quarantined traces excluded from retrieval path

### Idle sleep consolidation
- background idle loop in sidecar
- pipeline includes:
  - strength decay
  - prune low-value
  - dedup merge
  - capsule refresh
  - conflict refresh
  - preference/policy refresh

### Plugin runtime integration updates
- lifecycle registration fixed to `api.on(...)`
- per-turn `idle_tick` call
- per-turn `profile` pull
- critical preference pinning into MEMCTX (surface priority)
- user prompt preference event extraction -> sidecar `/preference/event`

## 7) Files changed (core subset)
- `sidecar/minisidecar.py`
- `sidecar/memq_sidecar/app.py`
- `plugin/openclaw-memory-memq/src/index.ts`
- `plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
- `plugin/openclaw-memory-memq/src/hooks/agent_end.ts`
- `plugin/openclaw-memory-memq/src/services/sidecar.ts`
- `plugin/openclaw-memory-memq/src/memq_core.ts`
- `bench/scripts/test_modea_hardening.py`
- `bench/scripts/check_memctx_budget.py`

## 8) Guarantee statement
The following is now verified on this local environment:
- OpenClaw is actively executing MEMQ Mode A hook logic per turn
- Sidecar endpoints are being called from OpenClaw runtime
- Quarantine, profile learning, consolidation, and conflict refresh are operational
- MEMCTX budget guard is active and separately auditable from provider usage outliers

## 9) Dual Audit (High-risk only) Verification

### What was added
- Primary deterministic audit always runs (regex/whitelist/weighted risk)
- Secondary LLM audit runs only when `riskScore >= threshold`
- New counters in stats:
  - `secondaryAuditCalled`
  - `secondaryAuditBlocked`

### Repro steps used
1. Start mock LLM audit endpoint on localhost (`/v1/chat/completions`)
2. Start sidecar with:
   - `MEMQ_LLM_AUDIT_ENABLED=1`
   - `MEMQ_LLM_AUDIT_THRESHOLD=0.75`
   - `MEMQ_LLM_AUDIT_URL=http://127.0.0.1:18999/v1/chat/completions`
   - `MEMQ_LLM_AUDIT_MODEL=mock-guard`
3. Run:
   - `python3 bench/scripts/test_dual_audit.py --expect-secondary`

### Observed result
- low-risk sample:
  - `passed=true`
  - `secondary.called=false`
- high-risk sample (secret + override phrase):
  - `passed=false`
  - `riskScore=1.0`
  - `secondary.called=true`
  - `secondary.block=true`
- counters:
  - `/audit/stats` returned non-zero `secondaryCalled` and `secondaryBlocked`
