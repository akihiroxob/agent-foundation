#!/usr/bin/env bash
# shellcheck shell=bash

PROVIDER_RESET_PARSER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/limit_reset.py"

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

  if [[ "${GITHUB_ENABLED:-false}" == true ]]; then
    args+=(--mcp-config "$project_root/.ralph/mcp.json" --strict-mcp-config)
  fi

  (
    cd "$project_root"
    exec "$CLAUDE_BIN" "${args[@]}" >"$output_pipe" 2>"$error_pipe"
  ) &
  PROVIDER_PID=$!
  if wait "$PROVIDER_PID"; then
    provider_status=0
  else
    provider_status=$?
  fi
  PROVIDER_PID=""
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

provider_reset_retry_seconds() { python3 "$PROVIDER_RESET_PARSER" "$@"; }

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
