#!/usr/bin/env bash
# shellcheck shell=bash

CLAUDE_BIN="$(jq -r '.claude.command // "claude"' "$RALPH_CONFIG_PATH")"
CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS="$(jq -r '.claude.dangerouslySkipPermissions // false' "$RALPH_CONFIG_PATH")"

provider_requirements() {
  require_command "$CLAUDE_BIN"
  case "$CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS" in
    true | false) ;;
    *)
      printf 'claude.dangerouslySkipPermissionsはbooleanで指定してください。\n' >&2
      exit 1
      ;;
  esac
}

provider_run() {
  local prompt_path="$1"
  local project_root="$2"
  local -a args=(--verbose -p "$(<"$prompt_path")")

  if [[ "$CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS" == true ]]; then
    args=(--dangerously-skip-permissions "${args[@]}")
  fi

  (
    cd "$project_root"
    "$CLAUDE_BIN" "${args[@]}"
  )
}
