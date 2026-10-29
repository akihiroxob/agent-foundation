#!/usr/bin/env bash
# shellcheck shell=bash

RALPH_MCP_ADAPTER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mcp_config.py"

ralph_mcp_arguments() {
  local provider="$1" project_root="$2" config_path arguments_path argument
  RALPH_MCP_ARGS=()
  config_path="$project_root/.mcp.json"
  if [[ "${GITHUB_ENABLED:-false}" == true ]]; then
    config_path="$project_root/.ralph/mcp.json"
    if [[ ! -f "$config_path" ]]; then
      printf 'Prepared Ralph MCP configuration is missing: %s\n' "$config_path" >&2
      return 1
    fi
  fi
  arguments_path="$(mktemp "${TMPDIR:-/tmp}/ralph-mcp-arguments.XXXXXX")" || return 1
  if ! python3 "$RALPH_MCP_ADAPTER" --provider "$provider" --config "$config_path" >"$arguments_path"; then
    rm -f "$arguments_path"
    return 1
  fi
  while IFS= read -r -d '' argument; do
    RALPH_MCP_ARGS+=("$argument")
  done <"$arguments_path"
  rm -f "$arguments_path"
}
