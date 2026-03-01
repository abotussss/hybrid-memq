#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ID="openclaw-memory-memq"
PLUGIN_PATH="$ROOT_DIR/plugin/openclaw-memory-memq"
STATE_DIR="$ROOT_DIR/.memq"
STATE_FILE="$STATE_DIR/openclaw_switch_state.json"
PID_FILE="$STATE_DIR/minisidecar.pid"
SIDECAR_ENV="$STATE_DIR/sidecar.env"
SIDECAR_LOG="/tmp/memq-v2-sidecar.log"
SUPERVISOR_LOG="/tmp/memq-v2-sidecar-supervisor.log"

mkdir -p "$STATE_DIR"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

need_cmd openclaw
need_cmd python3
need_cmd pnpm

print_banner() {
  cat <<'EOF'
 __  __ ______ __  __  ___
|  \/  |  ____|  \/  |/ _ \
| \  / | |__  | \  / | | | |
| |\/| |  __| | |\/| | | | |
| |  | | |____| |  | | |_| |
|_|  |_|______|_|  |_|\___/
Hybrid MEMQ v2 :: MEMRULES + MEMSTYLE + MEMCTX
EOF
}

get_plugins_json() {
  openclaw config get plugins 2>/dev/null || echo '{}'
}

save_switch_state() {
  local before_json="$1"
  python3 - "$STATE_FILE" "$PLUGIN_ID" "$before_json" <<'PY'
import json,sys,time,os
path,pid,raw=sys.argv[1],sys.argv[2],sys.argv[3]
try: obj=json.loads(raw)
except Exception: obj={}
out={
  "saved_at": int(time.time()),
  "plugin_id": pid,
  "plugins_before": obj,
  "previous_memory_slot": ((obj or {}).get("slots") or {}).get("memory"),
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
  json.dump(out, f, ensure_ascii=False, indent=2)
PY
}

build_enabled_plugins_json() {
  local before_json="$1"
  python3 - "$PLUGIN_ID" "$PLUGIN_PATH" "$before_json" <<'PY'
import json,sys
pid,pp,raw=sys.argv[1],sys.argv[2],sys.argv[3]
try: obj=json.loads(raw)
except Exception: obj={}
if not isinstance(obj,dict): obj={}
allow=obj.get("allow") or []
if pid not in allow: allow.append(pid)
load=obj.get("load") or {}
paths=load.get("paths") or []
if pp not in paths: paths.append(pp)
load["paths"]=paths
entries=obj.get("entries") or {}
entries.setdefault(pid,{})
entries[pid]["enabled"]=True
slots=obj.get("slots") or {}
slots["memory"]=pid
obj["allow"]=allow
obj["load"]=load
obj["entries"]=entries
obj["slots"]=slots
print(json.dumps(obj,separators=(",",":")))
PY
}

set_plugin_cfg_key() {
  local key="$1"
  local value_json="$2"
  local cur next
  cur="$(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  next="$(python3 - "$cur" "$key" "$value_json" <<'PY'
import json,sys
raw,key,val_raw=sys.argv[1],sys.argv[2],sys.argv[3]
try: obj=json.loads(raw)
except Exception: obj={}
if not isinstance(obj,dict): obj={}
try:
  val=json.loads(val_raw)
except Exception:
  val=val_raw
obj[key]=val
print(json.dumps(obj,separators=(",",":")))
PY
)"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$next" >/dev/null
}

cmd_install() {
  print_banner
  pnpm --dir "$PLUGIN_PATH" install >/dev/null
  pnpm --dir "$PLUGIN_PATH" build >/dev/null
  openclaw plugins install -l "$PLUGIN_PATH" >/dev/null
  echo "installed: $PLUGIN_ID"
}

cmd_reset_config() {
  local cfg
  cfg="$(python3 - <<'PY'
import json
cfg = {
  "memq.sidecarUrl": "http://127.0.0.1:7781",
  "memq.workspaceRoot": "__ROOT__",
  "memq.budgets.memctxTokens": 120,
  "memq.budgets.rulesTokens": 80,
  "memq.budgets.styleTokens": 120,
  "memq.recent.maxTokens": 5000,
  "memq.recent.minKeepMessages": 6,
  "memq.retrieval.topK": 5,
  "memq.retrieval.surfaceThreshold": 0.85,
  "memq.retrieval.deepEnabled": True,
  "memq.archive.enabled": True,
  "memq.archive.maxFileBytes": 8000000,
  "memq.archive.maxFiles": 30,
  "memq.degraded.enabled": True,
  "memq.security.primaryRulesEnabled": True,
  "memq.security.llmAuditEnabled": False,
  "memq.security.llmAuditThreshold": 0.2,
  "memq.security.blockThreshold": 0.85,
  "memq.style.enabled": True,
  "memq.style.maxBudgetTokens": 220,
  "memq.idle.enabled": True,
  "memq.idle.idleSeconds": 120,
}
print(json.dumps(cfg, separators=(',',':')))
PY
)"
  cfg="${cfg/__ROOT__/$ROOT_DIR}"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$cfg" >/dev/null
  echo "plugin config reset to v2 defaults"
}

cmd_enable() {
  local before merged
  before="$(get_plugins_json)"
  save_switch_state "$before"
  merged="$(build_enabled_plugins_json "$before")"
  openclaw config set plugins "$merged" >/dev/null
  echo "enabled memory slot: $PLUGIN_ID"
}

cmd_disable() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "no saved state: $STATE_FILE" >&2
    exit 1
  fi
  local restore prev_slot
  restore="$(python3 - "$STATE_FILE" <<'PY'
import json,sys
st=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(json.dumps(st.get("plugins_before",{}),separators=(",",":")))
PY
)"
  openclaw config set plugins "$restore" >/dev/null
  prev_slot="$(python3 - "$STATE_FILE" <<'PY'
import json,sys
st=json.load(open(sys.argv[1],"r",encoding="utf-8"))
v=st.get("previous_memory_slot")
print("" if v is None else str(v))
PY
)"
  if [[ -n "$prev_slot" ]]; then
    openclaw config set plugins.slots.memory "$prev_slot" >/dev/null || true
  fi
  echo "restored previous plugins config"
}

load_sidecar_env() {
  if [[ -f "$SIDECAR_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$SIDECAR_ENV"
  fi
}

choose_sidecar_python() {
  if [[ -x "$ROOT_DIR/sidecar/.venv/bin/python" ]]; then
    echo "$ROOT_DIR/sidecar/.venv/bin/python"
  else
    echo "python3"
  fi
}

kill_port_7781() {
  local pids
  pids="$(lsof -nP -iTCP:7781 -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    for pid in $pids; do
      kill "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
}

cmd_start_sidecar() {
  if curl -fsS http://127.0.0.1:7781/health >/dev/null 2>&1; then
    echo "sidecar already running"
    return
  fi
  local py
  py="$(choose_sidecar_python)"
  load_sidecar_env
  nohup env \
    MEMQ_ROOT="$ROOT_DIR" \
    MEMQ_DB_PATH=".memq/sidecar.sqlite3" \
    MEMQ_LLM_AUDIT_ENABLED="${MEMQ_LLM_AUDIT_ENABLED:-0}" \
    MEMQ_LLM_AUDIT_URL="${MEMQ_LLM_AUDIT_URL:-https://api.openai.com/v1/chat/completions}" \
    MEMQ_LLM_AUDIT_MODEL="${MEMQ_LLM_AUDIT_MODEL:-gpt-5.2}" \
    MEMQ_LLM_AUDIT_API_KEY="${MEMQ_LLM_AUDIT_API_KEY:-}" \
    MEMQ_LLM_AUDIT_TIMEOUT_SEC="${MEMQ_LLM_AUDIT_TIMEOUT_SEC:-20}" \
    "$py" "$ROOT_DIR/sidecar/supervisor.py" \
      --python "$py" \
      --app "$ROOT_DIR/sidecar/minisidecar.py" \
      --log "$SIDECAR_LOG" \
      --restart-delay-sec 1.0 >"$SUPERVISOR_LOG" 2>&1 < /dev/null &
  echo "$!" > "$PID_FILE"

  for _ in $(seq 1 25); do
    if curl -fsS http://127.0.0.1:7781/health >/dev/null 2>&1; then
      echo "sidecar started"
      return
    fi
    sleep 1
  done

  echo "failed to start sidecar; logs: $SIDECAR_LOG, $SUPERVISOR_LOG" >&2
  tail -n 80 "$SIDECAR_LOG" >&2 || true
  tail -n 80 "$SUPERVISOR_LOG" >&2 || true
  exit 1
}

cmd_stop_sidecar() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # Best-effort cleanup in case PID file is stale or multiple supervisors were spawned.
  pkill -f "$ROOT_DIR/sidecar/supervisor.py" 2>/dev/null || true
  pkill -f "$ROOT_DIR/sidecar/minisidecar.py" 2>/dev/null || true
  kill_port_7781
  echo "sidecar stopped"
}

cmd_restart_sidecar() {
  cmd_stop_sidecar
  cmd_start_sidecar
}

cmd_status() {
  echo "plugin:"
  openclaw plugins list | rg -n "$PLUGIN_ID|Hybrid MEMQ|Memory MEMQ" -N || true
  echo "memory_slot: $(openclaw config get plugins.slots.memory 2>/dev/null || echo '<unset>')"
  echo "plugin_config: $(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  echo "sidecar_health: $(curl -sS http://127.0.0.1:7781/health 2>/dev/null || echo '{"ok":false}')"
}

cmd_audit_on() {
  local url model risk block
  url="${2:-}"
  model="${3:-}"
  risk="${4:-0.20}"
  block="${5:-0.85}"
  if [[ -z "$url" || -z "$model" ]]; then
    echo "usage: scripts/memq-openclaw.sh audit-on <url> <model> [risk_threshold] [block_threshold]" >&2
    exit 1
  fi

  set_plugin_cfg_key "memq.security.llmAuditEnabled" "true"
  set_plugin_cfg_key "memq.security.llmAuditThreshold" "$risk"
  set_plugin_cfg_key "memq.security.blockThreshold" "$block"

  {
    echo "MEMQ_ROOT=$ROOT_DIR"
    echo "MEMQ_DB_PATH=.memq/sidecar.sqlite3"
    echo "MEMQ_LLM_AUDIT_ENABLED=1"
    echo "MEMQ_LLM_AUDIT_URL=$url"
    echo "MEMQ_LLM_AUDIT_MODEL=$model"
    echo "MEMQ_LLM_AUDIT_TIMEOUT_SEC=20"
    echo "MEMQ_LLM_AUDIT_API_KEY=${MEMQ_LLM_AUDIT_API_KEY:-}"
  } > "$SIDECAR_ENV"

  cmd_restart_sidecar
  echo "secondary audit enabled"
}

cmd_audit_off() {
  set_plugin_cfg_key "memq.security.llmAuditEnabled" "false"
  {
    echo "MEMQ_ROOT=$ROOT_DIR"
    echo "MEMQ_DB_PATH=.memq/sidecar.sqlite3"
    echo "MEMQ_LLM_AUDIT_ENABLED=0"
    echo "MEMQ_LLM_AUDIT_URL=https://api.openai.com/v1/chat/completions"
    echo "MEMQ_LLM_AUDIT_MODEL=gpt-5.2"
    echo "MEMQ_LLM_AUDIT_TIMEOUT_SEC=20"
    echo "MEMQ_LLM_AUDIT_API_KEY="
  } > "$SIDECAR_ENV"
  cmd_restart_sidecar
  echo "secondary audit disabled"
}

cmd_audit_primary_on() {
  set_plugin_cfg_key "memq.security.primaryRulesEnabled" "true"
  echo "primary audit (rule-based) enabled"
}

cmd_audit_primary_off() {
  set_plugin_cfg_key "memq.security.primaryRulesEnabled" "false"
  echo "primary audit (rule-based) disabled"
}

cmd_audit_status() {
  echo "plugin audit config:"
  openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}'
  echo "sidecar env:"
  if [[ -f "$SIDECAR_ENV" ]]; then
    cat "$SIDECAR_ENV"
  else
    echo "<none>"
  fi
}

cmd_memstyle_on() {
  set_plugin_cfg_key "memq.style.enabled" "true"
  echo "memstyle enabled"
}

cmd_memstyle_off() {
  set_plugin_cfg_key "memq.style.enabled" "false"
  echo "memstyle disabled"
}

cmd_memstyle_status() {
  local cur
  cur="$(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  python3 - "$cur" <<'PY'
import json,sys
try: obj=json.loads(sys.argv[1])
except Exception: obj={}
print("memstyle.enabled:", obj.get("memq.style.enabled", "<unset>"))
PY
}

cmd_setup() {
  print_banner
  cmd_install
  cmd_reset_config
  cmd_start_sidecar
  cmd_enable
  cmd_status
}

cmd_quickstart() {
  cmd_setup
}

usage() {
  cat <<EOF
usage: scripts/memq-openclaw.sh <command>

commands:
  setup               install + start-sidecar + enable + status
  quickstart          same as setup
  install             install/link plugin into OpenClaw
  reset-config        reset plugin config to MEMQ v2 defaults
  enable              enable MEMQ memory slot (saves previous state)
  disable             restore previous plugin/memory-slot state
  start-sidecar       start local sidecar
  stop-sidecar        stop local sidecar
  restart-sidecar     restart local sidecar
  status              show plugin/slot/sidecar status
  audit-on <url> <model> [risk] [block]
                      enable secondary LLM audit (high-risk path)
  audit-off           disable secondary LLM audit
  audit-primary-on    enable primary rule-based output audit
  audit-primary-off   disable primary rule-based output audit
  audit-status        print audit settings
  memstyle-on         enable MEMSTYLE injection
  memstyle-off        disable MEMSTYLE injection
  memstyle-status     print MEMSTYLE enabled status
EOF
}

case "${1:-}" in
  setup) cmd_setup ;;
  quickstart) cmd_quickstart ;;
  install) cmd_install ;;
  reset-config) cmd_reset_config ;;
  enable) cmd_enable ;;
  disable) cmd_disable ;;
  start-sidecar) cmd_start_sidecar ;;
  stop-sidecar) cmd_stop_sidecar ;;
  restart-sidecar) cmd_restart_sidecar ;;
  status) cmd_status ;;
  audit-on) cmd_audit_on "$@" ;;
  audit-off) cmd_audit_off ;;
  audit-primary-on) cmd_audit_primary_on ;;
  audit-primary-off) cmd_audit_primary_off ;;
  audit-status) cmd_audit_status ;;
  memstyle-on) cmd_memstyle_on ;;
  memstyle-off) cmd_memstyle_off ;;
  memstyle-status) cmd_memstyle_status ;;
  *) usage; exit 1 ;;
esac
