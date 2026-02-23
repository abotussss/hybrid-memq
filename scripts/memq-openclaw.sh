#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ID="openclaw-memory-memq"
PLUGIN_PATH="$ROOT_DIR/plugin/openclaw-memory-memq"
STATE_DIR="$ROOT_DIR/.memq"
STATE_FILE="$STATE_DIR/openclaw_switch_state.json"
OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
OPENCLAW_CONFIG_BAK="$STATE_DIR/openclaw.json.backup"
PID_FILE="$STATE_DIR/minisidecar.pid"
SIDECAR_LOG="/tmp/memq-minisidecar.log"
SIDECAR_ENV="$STATE_DIR/sidecar.env"

mkdir -p "$STATE_DIR"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing command: $1" >&2
    exit 1
  }
}

need_cmd openclaw
need_cmd python3

json_merge_enable() {
  python3 - "$PLUGIN_ID" "$PLUGIN_PATH" <<'PY'
import json,sys
pid=sys.argv[1]
pp=sys.argv[2]
raw=sys.stdin.read().strip()
obj={}
if raw:
    try: obj=json.loads(raw)
    except Exception: obj={}
if not isinstance(obj,dict): obj={}
allow=obj.get('allow') or []
if pid not in allow: allow.append(pid)
load=obj.get('load') or {}
paths=load.get('paths') or []
if pp not in paths: paths.append(pp)
load['paths']=paths
entries=obj.get('entries') or {}
entries.setdefault(pid,{})
entries[pid]['enabled']=True
slots=obj.get('slots') or {}
slots['memory']=pid
obj['allow']=allow
obj['load']=load
obj['entries']=entries
obj['slots']=slots
print(json.dumps(obj,separators=(',',':')))
PY
}

json_restore_from_state() {
  python3 - "$STATE_FILE" <<'PY'
import json,sys,os
sf=sys.argv[1]
if not os.path.exists(sf):
    print('{}')
    sys.exit(0)
with open(sf,'r',encoding='utf-8') as f:
    st=json.load(f)
print(json.dumps(st.get('plugins_before',{}),separators=(',',':')))
PY
}

save_state() {
  python3 - "$STATE_FILE" "$PLUGIN_ID" <<'PY'
import json,sys,time,os
sf=sys.argv[1]
pid=sys.argv[2]
raw=sys.stdin.read().strip()
obj={}
if raw:
    try: obj=json.loads(raw)
    except Exception: obj={}
if not isinstance(obj,dict):
    obj={}
slots=(obj.get('slots') or {}) if isinstance(obj,dict) else {}
prev_slot=slots.get('memory')
out={'saved_at':int(time.time()),'plugins_before':obj,'previous_memory_slot':prev_slot,'plugin_id':pid}
os.makedirs(os.path.dirname(sf),exist_ok=True)
with open(sf,'w',encoding='utf-8') as f:
    json.dump(out,f,ensure_ascii=True,indent=2)
print(sf)
PY
}

cmd_install() {
  openclaw plugins install -l "$PLUGIN_PATH" >/dev/null
  echo "installed: $PLUGIN_ID"
}

cmd_enable() {
  if [[ -f "$OPENCLAW_CONFIG" ]]; then
    cp "$OPENCLAW_CONFIG" "$OPENCLAW_CONFIG_BAK"
  fi
  local before
  before="$(openclaw config get plugins 2>/dev/null || echo '{}')"
  printf '%s' "$before" | save_state >/dev/null
  local merged
  merged="$(printf '%s' "$before" | json_merge_enable)"
  openclaw config set plugins "$merged" >/dev/null
  echo "enabled memory slot: $PLUGIN_ID"
}

cmd_disable() {
  if [[ -f "$OPENCLAW_CONFIG_BAK" ]]; then
    cp "$OPENCLAW_CONFIG_BAK" "$OPENCLAW_CONFIG"
    echo "restored previous OpenClaw config file from backup"
    return
  fi
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "no previous config snapshot: $STATE_FILE" >&2
    exit 1
  fi
  local restore
  restore="$(json_restore_from_state)"
  openclaw config set plugins "$restore" >/dev/null
  local prev_slot
  prev_slot="$(python3 - "$STATE_FILE" <<'PY'
import json,sys
st=json.load(open(sys.argv[1],'r',encoding='utf-8'))
v=st.get('previous_memory_slot')
print("" if v is None else str(v))
PY
)"
  if [[ -n "${prev_slot:-}" ]]; then
    openclaw config set plugins.slots.memory "$prev_slot" >/dev/null || true
  else
    openclaw config unset plugins.slots.memory >/dev/null || true
  fi
  echo "restored previous plugins config"
}

cmd_start_sidecar() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "sidecar already running (pid=$pid)"
      return
    fi
  fi
  if [[ -f "$SIDECAR_ENV" ]]; then
    # shellcheck disable=SC1090
    nohup /bin/bash -lc "set -a; source '$SIDECAR_ENV'; set +a; python3 '$ROOT_DIR/sidecar/minisidecar.py'" >"$SIDECAR_LOG" 2>&1 &
  else
    nohup python3 "$ROOT_DIR/sidecar/minisidecar.py" >"$SIDECAR_LOG" 2>&1 &
  fi
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "failed to start sidecar. log: $SIDECAR_LOG" >&2
    exit 1
  fi
  echo "sidecar started (pid=$pid)"
}

cmd_audit_on() {
  local url="${2:-}"
  local model="${3:-}"
  local threshold="${4:-0.20}"
  local block_threshold="${5:-0.85}"
  if [[ -z "$url" || -z "$model" ]]; then
    echo "usage: scripts/memq-openclaw.sh audit-on <llm_audit_url> <llm_audit_model> [llm_risk_threshold] [block_threshold]" >&2
    exit 1
  fi
  {
    echo "MEMQ_OUTPUT_AUDIT_ENABLED=1"
    echo "MEMQ_LLM_AUDIT_ENABLED=1"
    echo "MEMQ_LLM_AUDIT_URL=$url"
    echo "MEMQ_LLM_AUDIT_MODEL=$model"
    echo "MEMQ_LLM_AUDIT_THRESHOLD=$threshold"
    echo "MEMQ_AUDIT_BLOCK_THRESHOLD=$block_threshold"
    echo "MEMQ_AUDIT_LANG_ALWAYS_SECONDARY=1"
    echo "MEMQ_AUDIT_LANG_REPAIR_ENABLED=1"
  } > "$SIDECAR_ENV"
  cmd_stop_sidecar
  cmd_start_sidecar
  echo "dual audit enabled"
  cmd_audit_status
}

cmd_audit_off() {
  {
    echo "MEMQ_OUTPUT_AUDIT_ENABLED=1"
    echo "MEMQ_LLM_AUDIT_ENABLED=0"
    echo "MEMQ_LLM_AUDIT_URL="
    echo "MEMQ_LLM_AUDIT_MODEL="
    echo "MEMQ_LLM_AUDIT_THRESHOLD=0.20"
    echo "MEMQ_AUDIT_BLOCK_THRESHOLD=0.85"
    echo "MEMQ_AUDIT_LANG_ALWAYS_SECONDARY=1"
    echo "MEMQ_AUDIT_LANG_REPAIR_ENABLED=1"
  } > "$SIDECAR_ENV"
  cmd_stop_sidecar
  cmd_start_sidecar
  echo "dual audit disabled"
  cmd_audit_status
}

cmd_audit_primary_on() {
  mkdir -p "$STATE_DIR"
  touch "$SIDECAR_ENV"
  python3 - "$SIDECAR_ENV" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
env = {}
for line in p.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        env[k] = v
env["MEMQ_OUTPUT_AUDIT_ENABLED"] = "1"
p.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
PY
  cmd_stop_sidecar
  cmd_start_sidecar
  echo "primary output audit enabled"
  cmd_audit_status
}

cmd_audit_primary_off() {
  mkdir -p "$STATE_DIR"
  touch "$SIDECAR_ENV"
  python3 - "$SIDECAR_ENV" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
env = {}
for line in p.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        env[k] = v
env["MEMQ_OUTPUT_AUDIT_ENABLED"] = "0"
p.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
PY
  cmd_stop_sidecar
  cmd_start_sidecar
  echo "primary output audit disabled"
  cmd_audit_status
}

cmd_audit_status() {
  if [[ -f "$SIDECAR_ENV" ]]; then
    echo "sidecar_env_file: $SIDECAR_ENV"
    cat "$SIDECAR_ENV"
  else
    echo "sidecar_env_file: <none>"
  fi
}

cmd_stop_sidecar() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "sidecar pid file not found"
    return
  fi
  local pid
  pid="$(cat "$PID_FILE" || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    sleep 1
  fi
  rm -f "$PID_FILE"
  echo "sidecar stopped"
}

cmd_status() {
  echo "plugin: $(openclaw plugins list | rg -n "$PLUGIN_ID|Memory MEMQ" -N || true)"
  echo "memory_slot: $(openclaw config get plugins.slots.memory 2>/dev/null || echo '<unset>')"
  echo "sidecar_health: $(curl -sS http://127.0.0.1:7781/health 2>/dev/null || echo '{"ok":false}')"
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "sidecar_pid: $pid (running)"
    else
      echo "sidecar_pid: $pid (stale)"
    fi
  else
    echo "sidecar_pid: <none>"
  fi
}

cmd_quickstart() {
  cmd_install
  cmd_start_sidecar
  cmd_enable
  cmd_status
}

cmd_on() {
  cmd_quickstart
}

cmd_off() {
  cmd_disable
  cmd_stop_sidecar
  echo "memq disabled (slot restored, sidecar stopped)"
}

usage() {
  cat <<EOF
usage: scripts/memq-openclaw.sh <command>

commands:
  install          install plugin (linked)
  enable           switch OpenClaw memory slot to memq (snapshot old config)
  disable          restore previous OpenClaw plugins config from snapshot
  on               shortcut: quickstart
  off              shortcut: disable + stop-sidecar
  start-sidecar    start local minisidecar
  stop-sidecar     stop local minisidecar
  status           show plugin/slot/sidecar status
  audit-on         enable dual audit and restart sidecar
  audit-off        disable dual audit and restart sidecar
  audit-primary-on enable primary output audit and restart sidecar
  audit-primary-off disable primary output audit and restart sidecar
  audit-status     show current sidecar audit env settings
  quickstart       install + start-sidecar + enable + status
EOF
}

case "${1:-}" in
  install) cmd_install ;;
  enable) cmd_enable ;;
  disable) cmd_disable ;;
  on) cmd_on ;;
  off) cmd_off ;;
  start-sidecar) cmd_start_sidecar ;;
  stop-sidecar) cmd_stop_sidecar ;;
  status) cmd_status ;;
  audit-on) cmd_audit_on "$@" ;;
  audit-off) cmd_audit_off ;;
  audit-primary-on) cmd_audit_primary_on ;;
  audit-primary-off) cmd_audit_primary_off ;;
  audit-status) cmd_audit_status ;;
  quickstart) cmd_quickstart ;;
  *) usage; exit 1 ;;
esac
