# Mode A Completeness Report (Gap-Closure)

Date: 2026-02-23  
Scope: close the previously unverified points (Ephemeral, idle sleep consolidation, profile learning, quarantine, channel policy structure, switching behavior) and stabilize sidecar runtime.

Canonical evidence snapshot (tracked): `/Users/hiroyukimiyake/Documents/New project/docs/modea_completeness_evidence_20260223.json`

## 1. Stability Fixes Applied

### 1.1 Sidecar runtime hardening
- Added supervisor process:
  - `/Users/hiroyukimiyake/Documents/New project/sidecar/supervisor.py`
  - Automatically restarts `minisidecar.py` on unexpected exit.
- Improved bind robustness in sidecar server:
  - `/Users/hiroyukimiyake/Documents/New project/sidecar/minisidecar.py`
  - `allow_reuse_address=True` and bind retry loop.
- Improved CLI lifecycle controls:
  - `/Users/hiroyukimiyake/Documents/New project/scripts/memq-openclaw.sh`
  - health-wait retry on start, explicit restart command, stale listener cleanup, child PID visibility.

### 1.2 Sleep consolidation bug fix
- Root cause: `GET` endpoints were updating `last_activity_at`, so stats/health polling itself prevented idle detection.
- Fix:
  - Removed activity update from `do_GET`.
  - File: `/Users/hiroyukimiyake/Documents/New project/sidecar/minisidecar.py`

## 2. Added Verification Assets

- `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_modea_missing_points.py`
  - Verifies:
    - idle sleep consolidation auto-run
    - preference/policy profile learning
    - quarantine ingestion + retrieval exclusion
    - consolidation pipeline outputs (decay/merge/conflict/capsule)
    - Ephemeral removal behavior
- `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_mem_channels.mjs`
  - Verifies:
    - injection order in plugin (`MEMRULES -> MEMSTYLE -> MEMCTX`)
    - budget gating existence in source-level compilers
    - representative token envelope checks

## 3. Evidence Results

### 3.1 Missing-point validation (runtime)
Source: `/Users/hiroyukimiyake/Documents/New project/bench/results/modea_missing_points_verify_20260223.json`

Key outcomes:
- `ok=true`
- `auto_sleep_consolidation.ok=true`
  - `lastConsolidateAt: 0 -> 1771856884` (idle path confirmed)
- `profile_learning.ok=true`
  - learned keys include `tone`, `avoid_suggestions`
  - memory policy includes `retention.default`, `ttl.default_days`
- `quarantine.ok=true`
  - injection-like trace quarantined
  - quarantined trace excluded from `/index/search`
- `consolidation.ok=true`
  - summary includes `decayed=16`, `merged=6`, `conflict_groups=2`, `capsules=3`
  - `ephemeral_removed=true`

### 3.2 MEMRULES/MEMSTYLE/MEMCTX channel checks
Source: `/Users/hiroyukimiyake/Documents/New project/bench/results/mem_channels_verify_20260223.json`

Key outcomes:
- `ok=true`
- `order_ok=true` (prepend order verified from hook source)
- `source_has_budget_gates=true` (budget cutoff guard exists for all three compilers)
- Representative token checks under configured envelopes:
  - `rules_tokens=72 <= 80`
  - `memctx_tokens=78 <= 120`

### 3.3 OpenClaw switching restore check
Source: `/Users/hiroyukimiyake/Documents/New project/bench/results/switch_restore_verify_20260223.json`

Key outcomes:
- `ok=true`
- `full_restore=true`
- OpenClaw config file hash returned to identical value after `enable -> disable`.

### 3.4 Hook wiring validation (before_compaction / gateway_start)
Source: `/Users/hiroyukimiyake/Documents/New project/bench/results/hook_wiring_verify_20260223.json`

Key outcomes:
- `index_registers_before_compaction=true`
- `before_compaction_calls_rebuild=true`
- `index_registers_gateway_start_service=true`
- `gateway_start_health_check=true`
- `gateway_start_ingest_markdown=true`
- `gateway_start_starts_background_consolidation=true`

## 4. Mapping to Prior “Unverified” Items

### Now verified in this run
- Ephemeral effectiveness (prunable behavior confirmed)
- Idle sleep consolidation auto-execution
- Local preference/policy profile learning path
- Quarantine isolation + retrieval exclusion
- MEMRULES/MEMSTYLE/MEMCTX structural order and budget-gate presence
- OpenClaw config restore behavior for enable/disable roundtrip
- Hook wiring for `before_compaction` and `gateway_start`

### Still environment-dependent / partial
- Foreground `openclaw gateway run` runtime verification was unstable in this sandbox (gateway close 1006), so gateway_start runtime evidence here is from local plugin execution path plus hook wiring checks.
- `before_compaction` explicit runtime trigger is platform/flow dependent; this report confirms implementation wiring and rebuild call path.

## 5. Practical Conclusion

Mode A now has direct evidence for the previously missing core runtime claims (sleep consolidation, Ephemeral, quarantine, local profile learning) and includes sidecar hardening changes to reduce crash/restart fragility.
