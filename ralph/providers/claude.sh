#!/usr/bin/env bash
# shellcheck shell=bash

CLAUDE_BIN="${RALPH_AGENT_COMMAND:-$(jq -r '.claude.command // "claude"' "$RALPH_CONFIG_PATH")}"
CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS="$(jq -r '.claude.dangerouslySkipPermissions // false' "$RALPH_CONFIG_PATH")"

provider_requirements() {
  require_command "$CLAUDE_BIN"
  require_command mkfifo
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
  local error_log error_pipe provider_status tee_pid
  local -a args=(--verbose -p "$(<"$prompt_path")")

  if [[ -n "${RALPH_AGENT_MODEL:-}" ]]; then
    args=(--model "$RALPH_AGENT_MODEL" "${args[@]}")
  fi

  if [[ -n "${PROVIDER_ERROR_LOG:-}" ]]; then
    rm -f "$PROVIDER_ERROR_LOG"
  fi
  error_log="$(mktemp "${TMPDIR:-/tmp}/ralph-provider-error.XXXXXX")"
  error_pipe="${error_log}.pipe"
  mkfifo "$error_pipe"
  PROVIDER_ERROR_LOG="$error_log"

  tee "$error_log" <"$error_pipe" >&2 &
  tee_pid=$!

  if [[ "$CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS" == true ]]; then
    args=(--dangerously-skip-permissions "${args[@]}")
  fi

  if (
    cd "$project_root"
    "$CLAUDE_BIN" "${args[@]}" 2>"$error_pipe"
  ); then
    provider_status=0
  else
    provider_status=$?
  fi
  wait "$tee_pid" 2>/dev/null || true
  rm -f "$error_pipe"
  if (( provider_status == 0 )); then
    rm -f "$PROVIDER_ERROR_LOG"
    PROVIDER_ERROR_LOG=""
  fi
  return "$provider_status"
}

provider_hit_token_limit() {
  local match_status
  [[ -n "${PROVIDER_ERROR_LOG:-}" && -f "$PROVIDER_ERROR_LOG" ]] || return 1
  if grep -Eiq "you('|’)ve hit your .*limit|usage limit|session quota exhausted" "$PROVIDER_ERROR_LOG"; then
    match_status=0
  else
    match_status=1
  fi
  rm -f "$PROVIDER_ERROR_LOG"
  PROVIDER_ERROR_LOG=""
  return "$match_status"
}
