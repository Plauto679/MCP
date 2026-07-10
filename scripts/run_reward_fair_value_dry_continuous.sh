#!/usr/bin/env bash
set -u

RUN_NAME="${RUN_NAME:-reward_fair_value_dry_live}"
CYCLE_SECONDS="${CYCLE_SECONDS:-600}"
STOP_AFTER_CYCLES="${STOP_AFTER_CYCLES:-0}"
REWARD_MAX_MARKETS="${REWARD_MAX_MARKETS:-300}"
REWARD_MIN_DAILY_RATE="${REWARD_MIN_DAILY_RATE:-5.0}"
REWARD_MIN_VOLUME_24H="${REWARD_MIN_VOLUME_24H:-500.0}"
REWARD_MIN_HOURS_TO_END="${REWARD_MIN_HOURS_TO_END:-1.0}"
REWARD_ROWS_PER_SCAN="${REWARD_ROWS_PER_SCAN:-80}"
EXTERNAL_MAX_MARKETS="${EXTERNAL_MAX_MARKETS:-800}"
EXTERNAL_MIN_VOLUME_24H="${EXTERNAL_MIN_VOLUME_24H:-100.0}"
EXTERNAL_MAX_YES_SPREAD="${EXTERNAL_MAX_YES_SPREAD:-0.15}"
EXTERNAL_ROWS_PER_SCAN="${EXTERNAL_ROWS_PER_SCAN:-120}"
MAX_PAPER_EVENTS="${MAX_PAPER_EVENTS:-200000}"
PAPER_EVERY_CYCLES="${PAPER_EVERY_CYCLES:-1}"
ANALYSIS_EVERY_CYCLES="${ANALYSIS_EVERY_CYCLES:-3}"
MAX_SNAPSHOT_AGE_HOURS="${MAX_SNAPSHOT_AGE_HOURS:-24.0}"
MAX_HISTORY_MB="${MAX_HISTORY_MB:-128.0}"
PUBLISH_EVERY_CYCLES="${PUBLISH_EVERY_CYCLES:-0}"
PUBLISH_GIT="${PUBLISH_GIT:-0}"
PUBLISH_REMOTE="${PUBLISH_REMOTE:-origin}"
PUBLISH_BRANCH="${PUBLISH_BRANCH:-dry-run-telemetry}"
PUBLISH_OUTPUT_DIR="${PUBLISH_OUTPUT_DIR:-run_reports}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
  python_bin=".venv/bin/python"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
  python_bin=".venv/Scripts/python.exe"
else
  python_bin="python3"
fi

run_dir="data/${RUN_NAME}"
log_dir="${run_dir}/logs"
heartbeat_path="${run_dir}/heartbeat.json"
pid_path="${run_dir}/supervisor.pid"
supervisor_log="${run_dir}/supervisor.log"

mkdir -p "$log_dir"

if [[ -f "$pid_path" ]]; then
  old_pid="$(head -n 1 "$pid_path" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Continuous dry supervisor already appears to be running with pid=${old_pid}."
    exit 1
  fi
fi
echo "$$" > "$pid_path"

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  echo "$line" | tee -a "$supervisor_log"
}

rotate_if_large() {
  local path="$1"
  local max_mb="$2"
  [[ -f "$path" ]] || return 0
  "$python_bin" - "$path" "$max_mb" <<'PY'
import os
import shutil
import sys
from datetime import datetime

path = sys.argv[1]
max_mb = float(sys.argv[2])
if not os.path.exists(path) or os.path.getsize(path) < max_mb * 1024 * 1024:
    raise SystemExit(0)
directory = os.path.dirname(path)
archive_dir = os.path.join(directory, "archive")
os.makedirs(archive_dir, exist_ok=True)
base, ext = os.path.splitext(os.path.basename(path))
target = os.path.join(archive_dir, f"{base}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{ext}")
shutil.move(path, target)
print(f"rotated path={path} archived_to={target}")
PY
}

invoke_step() {
  local name="$1"
  shift
  local out_path="${log_dir}/${name}.last.out.log"
  local err_path="${log_dir}/${name}.last.err.log"
  log "step_start name=${name}"
  "$python_bin" "$@" >"$out_path" 2>"$err_path"
  local exit_code=$?
  log "step_done name=${name} exit=${exit_code} out=${out_path} err=${err_path}"
  return "$exit_code"
}

write_heartbeat() {
  local cycle="$1"
  local cycle_start="$2"
  local cycle_end="$3"
  local sleep_seconds="$4"
  local market_exit="$5"
  local external_exit="$6"
  local paper_exit="$7"
  local eval_exit="$8"
  local shadow_exit="$9"
  local analysis_exit="${10}"
  "$python_bin" - "$heartbeat_path" "$RUN_NAME" "$$" "$cycle" "$cycle_start" "$cycle_end" "$sleep_seconds" \
    "$market_exit" "$external_exit" "$paper_exit" "$eval_exit" "$shadow_exit" "$analysis_exit" <<'PY'
import json
import sys

(
    heartbeat_path,
    run_name,
    pid,
    cycle,
    cycle_start,
    cycle_end,
    sleep_seconds,
    market_exit,
    external_exit,
    paper_exit,
    eval_exit,
    shadow_exit,
    analysis_exit,
) = sys.argv[1:]

exit_codes = {
    "market_making_scan": int(market_exit),
    "external_fair_value_scan": int(external_exit),
    "market_making_paper": int(paper_exit),
    "reward_fair_value_evaluator": int(eval_exit),
    "shadow_paper_trader": int(shadow_exit),
}
if int(analysis_exit) >= 0:
    exit_codes["shadow_action_analysis"] = int(analysis_exit)

payload = {
    "run_name": run_name,
    "mode": "dry_research_only",
    "pid": int(pid),
    "cycle": int(cycle),
    "cycle_start": cycle_start,
    "cycle_end": cycle_end,
    "next_cycle_after_seconds": int(sleep_seconds),
    "exit_codes": exit_codes,
    "outputs": {
        "market_making_history": "data/market_making/candidate_history.csv",
        "external_fair_value_history": "data/external_fair_value/candidate_history.csv",
        "maker_paper_report": "data/market_making_paper_live/report.json",
        "evaluator_report": "data/reward_fair_value_evaluator_live/report.json",
        "shadow_paper_report": "data/shadow_paper_trader_live/report.json",
        "shadow_action_history": "data/reward_fair_value_evaluator_live/shadow_action_history.csv",
        "supervisor_log": f"data/{run_name}/supervisor.log",
    },
}
with open(heartbeat_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
PY
}

publish_snapshot() {
  local cycle="$1"
  invoke_step "publish_dry_run_snapshot" \
    research/publish_dry_run_snapshot.py \
    --run-name "$RUN_NAME" \
    --output-dir "$PUBLISH_OUTPUT_DIR"
  local publish_exit=$?
  if (( publish_exit != 0 )); then
    log "publish_snapshot_failed exit=${publish_exit}"
    return "$publish_exit"
  fi
  if [[ "$PUBLISH_GIT" != "1" ]]; then
    return 0
  fi
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "publish_git_skip reason=not_git_repo"
    return 0
  fi
  git add -f "$PUBLISH_OUTPUT_DIR" >/dev/null 2>&1
  if git diff --cached --quiet -- "$PUBLISH_OUTPUT_DIR"; then
    log "publish_git_skip reason=no_report_changes"
    return 0
  fi
  git commit -m "dry run telemetry cycle ${cycle}" -- "$PUBLISH_OUTPUT_DIR" >/dev/null 2>&1
  local commit_exit=$?
  if (( commit_exit != 0 )); then
    log "publish_git_commit_failed exit=${commit_exit}"
    return "$commit_exit"
  fi
  git push "$PUBLISH_REMOTE" "HEAD:${PUBLISH_BRANCH}" >/dev/null 2>&1
  local push_exit=$?
  if (( push_exit != 0 )); then
    log "publish_git_push_failed exit=${push_exit} remote=${PUBLISH_REMOTE} branch=${PUBLISH_BRANCH}"
    return "$push_exit"
  fi
  log "publish_git_done remote=${PUBLISH_REMOTE} branch=${PUBLISH_BRANCH}"
  return 0
}

log "continuous dry supervisor starting pid=$$ cycle_seconds=${CYCLE_SECONDS} stop_after_cycles=${STOP_AFTER_CYCLES}"
log "safety mode=dry_research_only no_auth no_orders"

cycle=0
while true; do
  cycle=$((cycle + 1))
  cycle_start="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  cycle_start_epoch="$(date +%s)"
  log "cycle_start cycle=${cycle}"

  rotate_if_large "data/market_making/candidate_history.csv" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/market_making/scan_log.jsonl" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/external_fair_value/candidate_history.csv" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/external_fair_value/scan_log.jsonl" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/reward_fair_value_evaluator_live/shadow_action_history.csv" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/shadow_paper_trader_live/paper_orders.csv" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "data/shadow_paper_trader_live/paper_order_events.csv" "$MAX_HISTORY_MB" | while read -r line; do log "$line"; done
  rotate_if_large "$supervisor_log" "32.0" | while read -r line; do log "$line"; done

  invoke_step "market_making_scan" \
    research/market_making_scanner.py \
    --output-dir data/market_making \
    --max-markets "$REWARD_MAX_MARKETS" \
    --request-timeout-s 30 \
    --min-daily-rate "$REWARD_MIN_DAILY_RATE" \
    --min-volume-24hr "$REWARD_MIN_VOLUME_24H" \
    --min-hours-to-end "$REWARD_MIN_HOURS_TO_END" \
    --max-history-rows-per-scan "$REWARD_ROWS_PER_SCAN" \
    --iterations 1
  market_exit=$?

  invoke_step "external_fair_value_scan" \
    research/external_fair_value_scanner.py \
    --output-dir data/external_fair_value \
    --max-markets "$EXTERNAL_MAX_MARKETS" \
    --request-timeout-s 30 \
    --min-volume-24hr "$EXTERNAL_MIN_VOLUME_24H" \
    --max-yes-spread "$EXTERNAL_MAX_YES_SPREAD" \
    --max-history-rows-per-scan "$EXTERNAL_ROWS_PER_SCAN" \
    --iterations 1
  external_exit=$?

  paper_exit=0
  if (( PAPER_EVERY_CYCLES > 0 && cycle % PAPER_EVERY_CYCLES == 0 )); then
    invoke_step "market_making_paper" \
      research/market_making_paper_simulator.py \
      --history-csv data/market_making/candidate_history.csv \
      --output-dir data/market_making_paper_live \
      --max-events "$MAX_PAPER_EVENTS"
    paper_exit=$?
  fi

  invoke_step "reward_fair_value_evaluator" \
    research/reward_fair_value_evaluator.py \
    --maker-history-csv data/market_making/candidate_history.csv \
    --external-history-csv data/external_fair_value/candidate_history.csv \
    --maker-paper-events-csv data/market_making_paper_live/maker_paper_events.csv \
    --output-dir data/reward_fair_value_evaluator_live \
    --max-snapshot-age-hours "$MAX_SNAPSHOT_AGE_HOURS"
  eval_exit=$?

  invoke_step "shadow_paper_trader" \
    research/shadow_paper_trader.py \
    --shadow-actions-csv data/reward_fair_value_evaluator_live/shadow_actions.csv \
    --maker-history-csv data/market_making/candidate_history.csv \
    --output-dir data/shadow_paper_trader_live
  shadow_exit=$?

  analysis_exit=-1
  if (( ANALYSIS_EVERY_CYCLES > 0 && cycle % ANALYSIS_EVERY_CYCLES == 0 )); then
    invoke_step "shadow_action_analysis" \
      research/analyze_shadow_actions.py \
      --history-csv data/reward_fair_value_evaluator_live/shadow_action_history.csv \
      --output-dir data/shadow_action_analysis_live
    analysis_exit=$?
  fi

  if (( PUBLISH_EVERY_CYCLES > 0 && cycle % PUBLISH_EVERY_CYCLES == 0 )); then
    publish_snapshot "$cycle"
  fi

  cycle_end="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  elapsed=$(( $(date +%s) - cycle_start_epoch ))
  sleep_seconds=$(( CYCLE_SECONDS - elapsed ))
  if (( sleep_seconds < 5 )); then
    sleep_seconds=5
  fi
  write_heartbeat "$cycle" "$cycle_start" "$cycle_end" "$sleep_seconds" \
    "$market_exit" "$external_exit" "$paper_exit" "$eval_exit" "$shadow_exit" "$analysis_exit"
  log "cycle_done cycle=${cycle} elapsed_seconds=${elapsed} sleep_seconds=${sleep_seconds}"

  if (( STOP_AFTER_CYCLES > 0 && cycle >= STOP_AFTER_CYCLES )); then
    log "stop_after_cycles reached; exiting."
    break
  fi
  sleep "$sleep_seconds"
done
