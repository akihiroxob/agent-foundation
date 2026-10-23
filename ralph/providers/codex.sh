#!/usr/bin/env bash
# shellcheck shell=bash

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/mcp_config.sh"

PROVIDER_RESET_PARSER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/limit_reset.py"

CODEX_BIN="${RALPH_AGENT_COMMAND:-$(jq -r '.codex.command // "codex"' "$RALPH_CONFIG_PATH")}"
CODEX_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX="$(jq -r '.codex.dangerouslyBypassApprovalsAndSandbox // false' "$RALPH_CONFIG_PATH")"

provider_requirements() {
  require_command "$CODEX_BIN"
  require_command python3
  require_command mkfifo
  require_command tee
  case "$CODEX_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX" in
    true | false) ;;
    *)
      ralph_log_error 'codex.dangerouslyBypassApprovalsAndSandboxはbooleanで指定してください。\n'
      exit 1
      ;;
  esac
}

provider_run() {
  local prompt_path="$1"
  local project_root="$2"
  local error_log error_pipe provider_status tee_pid wacha_url_toml
  local -a args=(
    exec
    --ephemeral
    --color never
  )

  ralph_mcp_arguments codex "$project_root" || return 1
  args+=(${RALPH_MCP_ARGS[@]+"${RALPH_MCP_ARGS[@]}"})

  if [[ -n "${RALPH_AGENT_MODEL:-}" ]]; then
    args+=(--model "$RALPH_AGENT_MODEL")
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

  if [[ "$CODEX_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX" == true ]]; then
    args+=(--dangerously-bypass-approvals-and-sandbox)
  else
    args+=(--sandbox workspace-write)
  fi

  wacha_url_toml="$(jq -Rn --arg value "$WACHA_MCP_URL" '$value')"
  args+=(
    -c "mcp_servers.wacha.url=$wacha_url_toml"
    -c 'mcp_servers.wacha.bearer_token_env_var="WACHA_AGENT_NAME"'
    -c 'mcp_servers.wacha.default_tools_approval_mode="approve"'
    -c 'mcp_servers.wacha.required=true'
    -
  )

  (
    cd "$project_root"
    local -a timeout_command=()
    if (( ${RALPH_RECOVERY_TIMEOUT_SECONDS:-0} > 0 )); then
      timeout_command=(python3 "$(dirname "$PROVIDER_RESET_PARSER")/agent_timeout.py" "$RALPH_RECOVERY_TIMEOUT_SECONDS")
    fi
    exec ${timeout_command[@]+"${timeout_command[@]}"} "$CODEX_BIN" "${args[@]}" <"$prompt_path" 2>"$error_pipe"
  ) &
  PROVIDER_PID=$!
  if declare -F ralph_agent_started >/dev/null; then ralph_agent_started "$PROVIDER_PID"; fi
  if wait "$PROVIDER_PID"; then
    provider_status=0
  else
    provider_status=$?
  fi
  PROVIDER_PID=""
  if declare -F ralph_agent_finished >/dev/null; then ralph_agent_finished "$provider_status"; fi
  wait "$tee_pid" 2>/dev/null || true
  rm -f "$error_pipe"
  if (( provider_status == 0 )); then
    rm -f "$PROVIDER_ERROR_LOG"
    PROVIDER_ERROR_LOG=""
  fi
  return "$provider_status"
}

provider_hit_token_limit() {
  local match_status reset_seconds
  PROVIDER_TOKEN_LIMIT_RESET_PARSED=false
  PROVIDER_TOKEN_LIMIT_RETRY_SECONDS=""
  [[ -n "${PROVIDER_ERROR_LOG:-}" && -f "$PROVIDER_ERROR_LOG" ]] || return 1
  if grep -Eiq "usage limit|rate limit|quota (has been )?exceeded|too many requests|limit (has been )?reached" "$PROVIDER_ERROR_LOG"; then
    match_status=0
    if reset_seconds="$(python3 "$PROVIDER_RESET_PARSER" "$PROVIDER_ERROR_LOG" 2>/dev/null)"; then
      PROVIDER_TOKEN_LIMIT_RESET_PARSED=true
      PROVIDER_TOKEN_LIMIT_RETRY_SECONDS="$reset_seconds"
    fi
  else
    match_status=1
  fi
  rm -f "$PROVIDER_ERROR_LOG"
  PROVIDER_ERROR_LOG=""
  return "$match_status"
}
