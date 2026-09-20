#!/usr/bin/env bash
# shellcheck shell=bash

CODEX_BIN="${RALPH_AGENT_COMMAND:-$(jq -r '.codex.command // "codex"' "$RALPH_CONFIG_PATH")}"
CODEX_DANGEROUSLY_BYPASS_APPROVALS_AND_SANDBOX="$(jq -r '.codex.dangerouslyBypassApprovalsAndSandbox // false' "$RALPH_CONFIG_PATH")"

provider_requirements() {
  require_command "$CODEX_BIN"
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

  if (
    cd "$project_root"
    "$CODEX_BIN" "${args[@]}" <"$prompt_path" 2>"$error_pipe"
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
  if grep -Eiq "usage limit|rate limit|quota (has been )?exceeded|too many requests|limit (has been )?reached" "$PROVIDER_ERROR_LOG"; then
    match_status=0
  else
    match_status=1
  fi
  rm -f "$PROVIDER_ERROR_LOG"
  PROVIDER_ERROR_LOG=""
  return "$match_status"
}
