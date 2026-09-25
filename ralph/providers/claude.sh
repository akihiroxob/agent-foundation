#!/usr/bin/env bash
# shellcheck shell=bash

CLAUDE_BIN="${RALPH_AGENT_COMMAND:-$(jq -r '.claude.command // "claude"' "$RALPH_CONFIG_PATH")}"
CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS="$(jq -r '.claude.dangerouslySkipPermissions // false' "$RALPH_CONFIG_PATH")"

provider_requirements() {
  require_command "$CLAUDE_BIN"
  require_command mkfifo
  require_command python3
  require_command tee
  case "$CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS" in
    true | false) ;;
    *)
      ralph_log_error 'claude.dangerouslySkipPermissionsはbooleanで指定してください。\n'
      exit 1
      ;;
  esac
}

provider_run() {
  local prompt_path="$1"
  local project_root="$2"
  local error_log error_pipe error_tee_pid output_log output_pipe output_tee_pid provider_status
  local -a args=(--verbose -p "$(<"$prompt_path")")

  if [[ -n "${RALPH_AGENT_MODEL:-}" ]]; then
    args=(--model "$RALPH_AGENT_MODEL" "${args[@]}")
  fi

  if [[ -n "${PROVIDER_ERROR_LOG:-}" ]]; then
    rm -f "$PROVIDER_ERROR_LOG"
  fi
  if [[ -n "${PROVIDER_OUTPUT_LOG:-}" ]]; then
    rm -f "$PROVIDER_OUTPUT_LOG"
  fi
  error_log="$(mktemp "${TMPDIR:-/tmp}/ralph-provider-error.XXXXXX")"
  error_pipe="${error_log}.pipe"
  output_log="$(mktemp "${TMPDIR:-/tmp}/ralph-provider-output.XXXXXX")"
  output_pipe="${output_log}.pipe"
  mkfifo "$error_pipe" "$output_pipe"
  PROVIDER_ERROR_LOG="$error_log"
  PROVIDER_OUTPUT_LOG="$output_log"

  tee "$error_log" <"$error_pipe" >&2 &
  error_tee_pid=$!
  tee "$output_log" <"$output_pipe" &
  output_tee_pid=$!

  if [[ "$CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS" == true ]]; then
    args=(--dangerously-skip-permissions "${args[@]}")
  fi

  if (
    cd "$project_root"
    "$CLAUDE_BIN" "${args[@]}" >"$output_pipe" 2>"$error_pipe"
  ); then
    provider_status=0
  else
    provider_status=$?
  fi
  wait "$error_tee_pid" 2>/dev/null || true
  wait "$output_tee_pid" 2>/dev/null || true
  rm -f "$error_pipe" "$output_pipe"
  if (( provider_status == 0 )); then
    rm -f "$PROVIDER_ERROR_LOG" "$PROVIDER_OUTPUT_LOG"
    PROVIDER_ERROR_LOG=""
    PROVIDER_OUTPUT_LOG=""
  fi
  return "$provider_status"
}

provider_reset_retry_seconds() {
  python3 - "$@" <<'PY'
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

text = "\n".join(Path(path).read_text(encoding="utf-8", errors="replace") for path in sys.argv[1:])
matches = re.findall(
    r"resets\s+(\d{1,2}:\d{2}\s*(?:am|pm))\s*\(([^()]+)\)",
    text,
    flags=re.IGNORECASE,
)
if not matches:
    raise SystemExit(1)

time_text, timezone_name = matches[-1]
timezone = ZoneInfo(timezone_name.strip())
now = datetime.now(timezone)
parsed_time = datetime.strptime(re.sub(r"\s+", "", time_text).upper(), "%I:%M%p")
reset_at = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
if reset_at <= now:
    reset_at += timedelta(days=1)

print(max(1, math.ceil((reset_at - now).total_seconds()) + 60))
PY
}

provider_hit_token_limit() {
  local match_status reset_seconds
  local -a capture_logs=()
  PROVIDER_TOKEN_LIMIT_RESET_PARSED=false
  PROVIDER_TOKEN_LIMIT_RETRY_SECONDS=""
  if [[ -n "${PROVIDER_OUTPUT_LOG:-}" && -f "$PROVIDER_OUTPUT_LOG" ]]; then
    capture_logs+=("$PROVIDER_OUTPUT_LOG")
  fi
  if [[ -n "${PROVIDER_ERROR_LOG:-}" && -f "$PROVIDER_ERROR_LOG" ]]; then
    capture_logs+=("$PROVIDER_ERROR_LOG")
  fi
  ((${#capture_logs[@]} > 0)) || return 1

  if grep -Eiq "you('|’)ve hit your .*limit|usage limit|session quota exhausted" "${capture_logs[@]}"; then
    match_status=0
    if reset_seconds="$(provider_reset_retry_seconds "${capture_logs[@]}" 2>/dev/null)"; then
      PROVIDER_TOKEN_LIMIT_RETRY_SECONDS="$reset_seconds"
      PROVIDER_TOKEN_LIMIT_RESET_PARSED=true
    fi
  else
    match_status=1
  fi
  rm -f "${capture_logs[@]}"
  PROVIDER_ERROR_LOG=""
  PROVIDER_OUTPUT_LOG=""
  return "$match_status"
}
