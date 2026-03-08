#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_ID="openclaw-memory-memq"
PLUGIN_DIR="$ROOT_DIR/plugin/openclaw-memory-memq"
STATE_DIR="$ROOT_DIR/.memq"
STATE_FILE="$STATE_DIR/openclaw_plugins_backup.json"
PID_FILE="$STATE_DIR/minisidecar.pid"
ENV_FILE="$STATE_DIR/sidecar.env"
SIDECAR_LOG="$STATE_DIR/minisidecar.log"

mkdir -p "$STATE_DIR"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

need_cmd openclaw
need_cmd pnpm
need_cmd python3
need_cmd curl

choose_python() {
  if [[ -x "$ROOT_DIR/sidecar/.venv/bin/python" ]]; then
    echo "$ROOT_DIR/sidecar/.venv/bin/python"
  else
    echo "python3"
  fi
}

current_plugins_json() {
  openclaw config get plugins 2>/dev/null || echo '{}'
}

save_plugins_backup() {
  current_plugins_json > "$STATE_FILE"
}

write_profile_env() {
  local mode="$1"
  local degraded="false"
  if [[ "$mode" == "brain-optional" ]]; then
    degraded="true"
  fi
  cat > "$ENV_FILE" <<ENV
export MEMQ_ROOT='$ROOT_DIR'
export MEMQ_DB_PATH='.memq/memq_v3.sqlite3'
export MEMQ_HOST='127.0.0.1'
export MEMQ_PORT='7781'
export MEMQ_TIMEZONE='Asia/Tokyo'
export MEMQ_BRAIN_ENABLED='1'
export MEMQ_BRAIN_MODE='$mode'
export MEMQ_BRAIN_PROVIDER='ollama'
export MEMQ_BRAIN_BASE_URL='http://127.0.0.1:11434'
export MEMQ_BRAIN_MODEL='gpt-oss:20b'
export MEMQ_BRAIN_KEEP_ALIVE='30m'
export MEMQ_BRAIN_TIMEOUT_MS='60000'
export MEMQ_BRAIN_MAX_TOKENS='320'
export MEMQ_BRAIN_INGEST_MAX_TOKENS='320'
export MEMQ_BRAIN_RECALL_MAX_TOKENS='192'
export MEMQ_BRAIN_MERGE_MAX_TOKENS='96'
export MEMQ_BRAIN_AUDIT_MAX_TOKENS='96'
export MEMQ_BRAIN_CONCURRENT='1'
export MEMQ_QCTX_TOKENS='500'
export MEMQ_QRULE_TOKENS='500'
export MEMQ_QSTYLE_TOKENS='500'
export MEMQ_TOTAL_MAX_INPUT_TOKENS='5200'
export MEMQ_TOTAL_RESERVE_TOKENS='1600'
export MEMQ_RECENT_TOKENS='1800'
export MEMQ_RECENT_MIN_KEEP_MESSAGES='4'
export MEMQ_TOP_K='5'
export MEMQ_ARCHIVE_ENABLED='1'
export MEMQ_IDLE_ENABLED='1'
export MEMQ_IDLE_BACKGROUND_ENABLED='0'
export MEMQ_IDLE_SECONDS='120'
export MEMQ_AUDIT_PRIMARY_ENABLED='1'
export MEMQ_AUDIT_SECONDARY_ENABLED='0'
export MEMQ_AUDIT_RISK_THRESHOLD='0.35'
export MEMQ_AUDIT_BLOCK_THRESHOLD='0.85'
export MEMQ_DEGRADED_ENABLED='$degraded'
ENV
}

load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

set_plugin_profile() {
  local mode="$1"
  local degraded="false"
  if [[ "$mode" == "brain-optional" ]]; then
    degraded="true"
  fi
  local cfg
  cfg="$(cat <<JSON
{"memq.sidecarUrl":"http://127.0.0.1:7781","memq.workspaceRoot":"$ROOT_DIR","memq.brain.mode":"$mode","memq.brain.provider":"ollama","memq.brain.baseUrl":"http://127.0.0.1:11434","memq.brain.model":"gpt-oss:20b","memq.brain.keepAlive":"30m","memq.brain.timeoutMs":60000,"memq.budgets.qctxTokens":500,"memq.budgets.qruleTokens":500,"memq.budgets.qstyleTokens":500,"memq.total.maxInputTokens":5200,"memq.total.reserveTokens":1600,"memq.recent.maxTokens":1800,"memq.recent.minKeepMessages":4,"memq.retrieval.topK":5,"memq.degraded.enabled":$degraded,"memq.style.enabled":true,"memq.idle.enabled":true,"memq.security.primaryRulesEnabled":true,"memq.security.llmAuditEnabled":false}
JSON
)"
  openclaw config set "plugins.entries.$PLUGIN_ID.config" "$cfg" >/dev/null
}

enable_plugin_slot() {
  local before merged
  before="$(current_plugins_json)"
  save_plugins_backup
  merged="$(python3 - <<PY
import json
pid = ${PLUGIN_ID@Q}
pp = ${PLUGIN_DIR@Q}
raw = ${before@Q}
obj = json.loads(raw) if raw.strip() else {}
if not isinstance(obj, dict):
    obj = {}
allow = obj.get("allow") or []
if pid not in allow:
    allow.append(pid)
load = obj.get("load") or {}
paths = load.get("paths") or []
if pp not in paths:
    paths.append(pp)
load["paths"] = paths
entries = obj.get("entries") or {}
entry = entries.get(pid) or {}
entry["enabled"] = True
entries[pid] = entry
slots = obj.get("slots") or {}
slots["memory"] = pid
obj["allow"] = allow
obj["load"] = load
obj["entries"] = entries
obj["slots"] = slots
print(json.dumps(obj, separators=(",", ":")))
PY
)"
  openclaw config set plugins "$merged" >/dev/null
}

restore_plugin_slot() {
  if [[ -f "$STATE_FILE" ]]; then
    openclaw config set plugins "$(cat "$STATE_FILE")" >/dev/null
  fi
}

stop_sidecar() {
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  local pids
  pids="$(lsof -nP -iTCP:7781 -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  fi
}

start_sidecar() {
  load_env_file
  stop_sidecar
  local py
  py="$(choose_python)"
  nohup "$py" -m sidecar.minisidecar > "$SIDECAR_LOG" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:7781/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "sidecar failed to start" >&2
  exit 1
}

cmd_install() {
  pnpm --dir "$PLUGIN_DIR" install
  pnpm --dir "$PLUGIN_DIR" build
  openclaw plugins install -l "$PLUGIN_DIR"
}

cmd_brain_required_on() {
  write_profile_env "brain-required"
  set_plugin_profile "brain-required"
  start_sidecar
}

cmd_brain_optional_on() {
  write_profile_env "brain-optional"
  set_plugin_profile "brain-optional"
  start_sidecar
}

cmd_status() {
  echo "plugin.slot: $(openclaw config get plugins.slots.memory 2>/dev/null || echo '<unset>')"
  echo "env.file: $ENV_FILE"
  [[ -f "$ENV_FILE" ]] && cat "$ENV_FILE"
  echo "--- health ---"
  curl -fsS http://127.0.0.1:7781/health || true
}

cmd_brain_proof() {
  echo "--- /brain/stats ---"
  curl -fsS http://127.0.0.1:7781/brain/stats
  echo
  echo "--- /brain/trace/recent?n=10 ---"
  curl -fsS 'http://127.0.0.1:7781/brain/trace/recent?n=10'
  echo
  echo "--- ollama /api/ps ---"
  curl -fsS http://127.0.0.1:11434/api/ps
}

cmd_setup() {
  cmd_install
  cmd_brain_required_on
  enable_plugin_slot
  cmd_status
}

case "${1:-status}" in
  install) cmd_install ;;
  enable) enable_plugin_slot ;;
  disable) restore_plugin_slot ;;
  start-sidecar) start_sidecar ;;
  stop-sidecar) stop_sidecar ;;
  restart-sidecar) stop_sidecar; start_sidecar ;;
  brain-required-on) cmd_brain_required_on ;;
  brain-optional-on) cmd_brain_optional_on ;;
  status) cmd_status ;;
  brain-proof) cmd_brain_proof ;;
  setup|quickstart) cmd_setup ;;
  *) echo "unknown command: ${1:-}" >&2; exit 1 ;;
esac
