#!/usr/bin/env bash
# shellcheck shell=bash
# WachaのStreamable HTTP MCPをステートレスに呼び出す。

WACHA_MCP_URL="$(jq -r '.wacha.url // "http://localhost:51743/mcp"' "$RALPH_CONFIG_PATH")"

backend_requirements() {
  require_command curl
}

normalize_mcp_response() {
  local response="$1"

  if printf '%s' "$response" | jq -e . >/dev/null 2>&1; then
    printf '%s\n' "$response"
    return
  fi

  # Streamable HTTPがSSEで返した場合は、最後のJSON data行を使う。
  printf '%s\n' "$response" | sed -n 's/^data: //p' | tail -n 1
}

mcp_call() {
  local tool_name="$1"
  local arguments_json="$2"
  local request raw response

  request="$(jq -cn \
    --arg name "$tool_name" \
    --argjson arguments "$arguments_json" \
    '{jsonrpc: "2.0", id: 1, method: "tools/call", params: {name: $name, arguments: $arguments}}')"
  raw="$(curl --silent --show-error --fail-with-body --request POST "$WACHA_MCP_URL" \
    --header 'Accept: application/json, text/event-stream' \
    --header 'Content-Type: application/json' \
    --header "Authorization: Bearer $WACHA_AGENT_NAME" \
    --data "$request")"
  response="$(normalize_mcp_response "$raw")"
  jq -e '.result and (.error | not)' >/dev/null <<<"$response" || {
    ralph_log_error 'Wacha MCP呼び出しに失敗しました (%s): %s\n' "$tool_name" "$response"
    exit 1
  }
  printf '%s\n' "$response"
}

backend_get_task_summary() {
  local role="$1"
  local availability project_id projects tasks
  if [[ "$role" == worker ]]; then
    availability="work"
  else
    availability="review"
  fi
  projects="$(mcp_call 'list_projects' '{}')"
  project_id="$(jq -r --arg name "$RALPH_PROJECT_NAME" \
    '.result.structuredContent.projects[] | select(.name == $name) | .id' <<<"$projects" | head -n 1)"
  [[ -n "$project_id" && "$project_id" != 'null' ]] || {
    ralph_log_error 'Wachaにプロジェクト %s が見つかりません。\n' "$RALPH_PROJECT_NAME"
    exit 1
  }

  tasks="$(mcp_call 'list_tasks' "$(jq -cn \
    --arg project_id "$project_id" \
    --arg availability "$availability" \
    '{projectId: $project_id, filter: {availableFor: $availability}, limit: 1}')")"
  jq -e '{
    byStatus: .result.structuredContent.summary.byStatus,
    availableCount: ((.result.structuredContent.tasks // []) | length)
  }' <<<"$tasks"
}

backend_print_status() {
  local role="$1"
  local summary="$2"
  local status
  if [[ "$role" == worker ]]; then
    status="$(jq -r '"Worker対象: todo=\(.byStatus.todo // 0) rejected=\(.byStatus.rejected // 0) doing=\(.byStatus.doing // 0) available=\(.availableCount // 0)"' <<<"$summary")"
  else
    status="$(jq -r '"Reviewer対象: in_review=\(.byStatus.in_review // 0) available=\(.availableCount // 0)"' <<<"$summary")"
  fi
  ralph_log '%s\n' "$status"
}

backend_pending_count() {
  local role="$1"
  local summary="$2"
  : "$role"
  jq -r '.availableCount // 0' <<<"$summary"
}
