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

if [[ -t 1 ]]; then
  C_RESET="$(printf '\033[0m')"
  C_CYAN="$(printf '\033[36m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
  C_DIM="$(printf '\033[2m')"
else
  C_RESET=""
  C_CYAN=""
  C_GREEN=""
  C_YELLOW=""
  C_DIM=""
fi

print_banner() {
  cat <<EOF
${C_CYAN} __  __ ______ __  __  ___
|  \/  |  ____|  \/  |/ _ \\
| \  / | |__  | \  / | | | |
| |\/| |  __| | |\/| | | | |
| |  | | |____| |  | | |_| |
|_|  |_|______|_|  |_|\__\_\\${C_RESET}
${C_GREEN}Hybrid MEMQ${C_RESET} :: Surface / Deep / Ephemeral
${C_DIM}MEMCTX + MEMRULES + MEMSTYLE for OpenClaw${C_RESET}
EOF
}

print_section() {
  echo ""
  echo "${C_YELLOW}== $1 ==${C_RESET}"
}

print_kv() {
  printf "  %-22s %s\n" "$1" "$2"
}

prompt_yes_no() {
  local prompt="$1"
  local default_yes="${2:-1}"
  local ans
  if [[ "$default_yes" == "1" ]]; then
    read -r -p "$prompt [Y/n]: " ans
    [[ -z "$ans" || "$ans" =~ ^[Yy]$ ]]
  else
    read -r -p "$prompt [y/N]: " ans
    [[ "$ans" =~ ^[Yy]$ ]]
  fi
}

set_plugin_config_key() {
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
print(json.dumps(obj,separators=(',',':')))
PY
)"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$next" >/dev/null
}

configure_secondary_audit() {
  local url model threshold block_threshold
  read -r -p "secondary audit url [https://api.openai.com/v1/chat/completions]: " url
  url="${url:-https://api.openai.com/v1/chat/completions}"
  read -r -p "secondary audit model [gpt-5.2]: " model
  model="${model:-gpt-5.2}"
  read -r -p "risk threshold [0.20]: " threshold
  threshold="${threshold:-0.20}"
  read -r -p "block threshold [0.85]: " block_threshold
  block_threshold="${block_threshold:-0.85}"
  cmd_audit_on "audit-on" "$url" "$model" "$threshold" "$block_threshold"
}

configure_memstyle_profile() {
  local tone persona speaking verbosity avoid strict
  read -r -p "style tone [keigo_friendly]: " tone
  tone="${tone:-keigo_friendly}"
  read -r -p "style persona [calm_pragmatic]: " persona
  persona="${persona:-calm_pragmatic}"
  read -r -p "style speakingStyle [clear_brief_actionable]: " speaking
  speaking="${speaking:-clear_brief_actionable}"
  read -r -p "style verbosity [low]: " verbosity
  verbosity="${verbosity:-low}"
  read -r -p "style avoid (| separated) [anime_style|translated_chinese_style_japanese]: " avoid
  avoid="${avoid:-anime_style|translated_chinese_style_japanese}"
  read -r -p "style strict? (y/N): " strict
  set_plugin_config_key "memq.style.enabled" "true"
  set_plugin_config_key "memq.style.tone" "$(python3 - <<PY
import json
print(json.dumps("$tone"))
PY
)"
  set_plugin_config_key "memq.style.persona" "$(python3 - <<PY
import json
print(json.dumps("$persona"))
PY
)"
  set_plugin_config_key "memq.style.speakingStyle" "$(python3 - <<PY
import json
print(json.dumps("$speaking"))
PY
)"
  set_plugin_config_key "memq.style.verbosity" "$(python3 - <<PY
import json
print(json.dumps("$verbosity"))
PY
)"
  set_plugin_config_key "memq.style.avoid" "$(python3 - <<PY
import json
print(json.dumps("$avoid"))
PY
)"
  if [[ "$strict" =~ ^[Yy]$ ]]; then
    set_plugin_config_key "memq.style.strict" "true"
  else
    set_plugin_config_key "memq.style.strict" "false"
  fi
  echo "memstyle profile updated"
}

cmd_configure() {
  print_banner
  echo "Interactive Configure"
  while true; do
    echo ""
    echo "Select:"
    echo "  1) Setup wizard (recommended)"
    echo "  2) Quickstart (install + start + enable)"
    echo "  3) Enable MEMQ"
    echo "  4) Disable MEMQ"
    echo "  5) Start sidecar"
    echo "  6) Stop sidecar"
    echo "  7) Primary audit ON"
    echo "  8) Primary audit OFF"
    echo "  9) Secondary audit configure (url/model)"
    echo " 10) Secondary audit OFF"
    echo " 11) MEMSTYLE ON"
    echo " 12) MEMSTYLE OFF"
    echo " 13) MEMSTYLE profile configure"
    echo " 14) Status"
    echo "  0) Exit"
    read -r -p "> " choice
    case "${choice:-}" in
      1) cmd_setup ;;
      2) cmd_quickstart ;;
      3) cmd_enable ;;
      4) cmd_disable ;;
      5) cmd_start_sidecar ;;
      6) cmd_stop_sidecar ;;
      7) cmd_audit_primary_on ;;
      8) cmd_audit_primary_off ;;
      9) configure_secondary_audit ;;
      10) cmd_audit_off ;;
      11) cmd_style_on ;;
      12) cmd_style_off ;;
      13) configure_memstyle_profile ;;
      14) cmd_status; cmd_audit_status; cmd_style_status ;;
      0) break ;;
      *) echo "invalid choice" ;;
    esac
  done
}

cmd_setup() {
  print_banner
  print_section "Setup Wizard"
  echo "This wizard configures MEMQ for local OpenClaw in a few steps."
  if prompt_yes_no "Install/link plugin into OpenClaw?" 1; then
    cmd_install
  fi
  if prompt_yes_no "Start sidecar now?" 1; then
    cmd_start_sidecar
  fi
  if prompt_yes_no "Switch memory slot to MEMQ now?" 1; then
    cmd_enable
  fi
  print_section "MEMRULES / Audit"
  if prompt_yes_no "Enable primary output audit (rule-based)?" 1; then
    cmd_audit_primary_on
  else
    cmd_audit_primary_off
  fi
  if prompt_yes_no "Enable secondary high-risk LLM audit?" 0; then
    configure_secondary_audit
  fi
  print_section "MEMSTYLE"
  if prompt_yes_no "Enable MEMSTYLE v1?" 0; then
    cmd_style_on
    if prompt_yes_no "Configure style profile now?" 1; then
      configure_memstyle_profile
    fi
  fi
  print_section "Current Status"
  cmd_status
  cmd_audit_status
  cmd_style_status
  echo ""
  echo "Done. You can rerun this anytime: scripts/memq-openclaw.sh setup"
}

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
  print_banner
  openclaw plugins install -l "$PLUGIN_PATH" >/dev/null
  echo "installed: $PLUGIN_ID"
  print_kv "plugin_id" "$PLUGIN_ID"
  print_kv "plugin_path" "$PLUGIN_PATH"
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
  if curl -fsS http://127.0.0.1:7781/health >/dev/null 2>&1; then
    echo "sidecar already running (detected by health endpoint)"
    if [[ ! -f "$PID_FILE" ]]; then
      local ext_pid
      ext_pid="$(lsof -nP -iTCP:7781 -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
      if [[ -n "${ext_pid:-}" ]]; then
        echo "$ext_pid" > "$PID_FILE"
      fi
    fi
    return
  fi
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
  local health
  health="$(curl -sS http://127.0.0.1:7781/health 2>/dev/null || echo '{"ok":false}')"
  echo "plugin: $(openclaw plugins list | rg -n "$PLUGIN_ID|Memory MEMQ" -N || true)"
  echo "memory_slot: $(openclaw config get plugins.slots.memory 2>/dev/null || echo '<unset>')"
  echo "sidecar_health: $health"
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "sidecar_pid: $pid (running)"
    else
      if echo "$health" | rg -q '"ok"\s*:\s*true'; then
        echo "sidecar_pid: $pid (stale, but sidecar is healthy/external)"
      else
        echo "sidecar_pid: $pid (stale)"
      fi
    fi
  else
    echo "sidecar_pid: <none>"
  fi
}

cmd_quickstart() {
  print_banner
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

cmd_style_on() {
  local cur
  cur="$(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  local next
  next="$(python3 - <<'PY' "$cur"
import json,sys
raw=sys.argv[1]
try: obj=json.loads(raw)
except Exception: obj={}
if not isinstance(obj,dict): obj={}
obj["memq.style.enabled"]=True
print(json.dumps(obj,separators=(',',':')))
PY
)"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$next" >/dev/null
  echo "memstyle enabled"
}

cmd_style_off() {
  local cur
  cur="$(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  local next
  next="$(python3 - <<'PY' "$cur"
import json,sys
raw=sys.argv[1]
try: obj=json.loads(raw)
except Exception: obj={}
if not isinstance(obj,dict): obj={}
obj["memq.style.enabled"]=False
print(json.dumps(obj,separators=(',',':')))
PY
)"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$next" >/dev/null
  echo "memstyle disabled"
}

cmd_style_status() {
  local cur v
  cur="$(openclaw config get "plugins.entries.$PLUGIN_ID.config" 2>/dev/null || echo '{}')"
  v="$(python3 - <<'PY' "$cur"
import json,sys
raw=sys.argv[1]
try: obj=json.loads(raw)
except Exception: obj={}
if not isinstance(obj,dict):
    print("<unset>")
else:
    val=obj.get("memq.style.enabled","<unset>")
    print(str(val).lower() if isinstance(val,bool) else str(val))
PY
)"
  echo "memstyle.enabled: $v"
}

usage() {
  cat <<EOF
usage: scripts/memq-openclaw.sh <command>

commands:
  setup            interactive setup wizard (recommended first run)
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
  memstyle-on      enable MEMSTYLE v1 injection
  memstyle-off     disable MEMSTYLE v1 injection
  memstyle-status  show MEMSTYLE v1 enabled status
  configure        interactive setup/config menu
  quickstart       install + start-sidecar + enable + status
EOF
}

case "${1:-}" in
  setup) cmd_setup ;;
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
  memstyle-on) cmd_style_on ;;
  memstyle-off) cmd_style_off ;;
  memstyle-status) cmd_style_status ;;
  configure) cmd_configure ;;
  quickstart) cmd_quickstart ;;
  *) usage; exit 1 ;;
esac
