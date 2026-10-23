"""Read-only execution visibility and local deferred-task filtering."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

if __package__:
    from .runtime_state import principal_for, project_root, read_object
else:
    from runtime_state import principal_for, project_root, read_object


def brief_reason(text: str) -> str:
    # Formatting only; the Agent's complete decision remains in the referenced record.
    for heading in ("### 不足・修正条件", "### 再開条件", "### 判定"):
        if heading in text:
            text = text.split(heading, 1)[1]
            break
    lines = [line.strip().lstrip("- ") for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return re.sub(r"\s+", " ", lines[0] if lines else "理由は保留記録を参照")[:240]


def deferred_records(root: Path, role: str):
    for path in (root / ".ralph/github-state").glob("*/deferred/*.json"):
        record = read_object(path)
        if record.get("deferred") and record.get("role") == role:
            yield path, record


def can_recheck(record: dict) -> bool:
    """Only a deliberate blocked result with a confirmed released Claim may wake automatically."""
    return (record.get("pendingResult", {}).get("decision") == "blocked"
        and record.get("releasedClaim") is True and not record.get("operationRecovery")
        and not record.get("finalCommand")
        and not any(record.get(key, {}).get("outcome") == "unknown" for key in ("prCreation", "reviewPosting"))
        and isinstance(record.get("deferredAt"), (int, float)))


def accepted_changes(record: dict, tasks: list[dict]) -> list[dict]:
    if not can_recheck(record):
        return []
    scoped = [task for task in tasks if task.get("projectId") == record["projectId"] and task.get("status") == "accepted"]
    if any(task.get("id") == record["task"]["id"] for task in scoped):
        return []  # Never restore work that the server already considers complete.
    return [{"id": task["id"], "title": task.get("title", ""), "updatedAt": task["updatedAt"]}
        for task in scoped if isinstance(task.get("updatedAt"), (int, float)) and task["updatedAt"] > record["deferredAt"]]


def accepted_tasks(workflow, records: list[dict]) -> list[dict]:
    tasks = []
    for project_id in sorted({record["projectId"] for record in records if can_recheck(record)}):
        response = workflow.mcp("list_tasks", {"projectId": project_id, "filter": {"status": ["accepted"]}})
        tasks.extend({**task, "projectId": project_id} for task in response["tasks"])
    return tasks


def filter_summary(root: Path, config: dict, role: str, summary: dict, accepted: list[dict] | None = None) -> dict:
    principal = principal_for(config, role)
    runtime = read_object(root / ".ralph/runtime.json") if (root / ".ralph/runtime.json").exists() else {}
    generation = runtime.get("agents", {}).get(principal, {}).get(role, {}).get("resumeGeneration", 0)
    deferred = []
    restoring = {}
    for path, record in deferred_records(root, role):
        if summary.get("projectId") and record.get("projectId") != summary["projectId"]:
            continue
        explicit = generation > record.get("deferredResumeGeneration", 0)
        if record.get("principal") == principal and (explicit or accepted_changes(record, accepted or [])):
            # Restoring a saved execution must run even while its own Claim hides it from availableFor.
            task = record["task"]
            restoring[task["id"]] = {"id": task["id"], "status": task.get("status", {"worker": "doing", "reviewer": "in_review", "manager": "wait_accept"}[role])}
            continue
        deferred.append({"taskId": record["task"]["id"], "title": record["task"].get("title", ""),
            "reason": brief_reason(record.get("deferredReason", "")), "recordPath": str(path)})
    blocked = {item["taskId"] for item in deferred}
    tasks = [task for task in summary.get("tasks", []) if task["id"] not in blocked]
    present = {task["id"] for task in tasks}
    tasks.extend(task for task_id, task in restoring.items() if task_id not in present and task_id not in blocked)
    return {**summary, "remoteAvailableCount": summary.get("availableCount", 0), "tasks": tasks,
        "availableCount": len(tasks), "availableRejected": sum(task["status"] == "rejected" for task in tasks),
        "deferredCount": len(deferred), "deferred": deferred}


def runner_alive(root: Path, principal: str, role: str) -> bool:
    path = root / ".ralph/runners" / principal / f"{role}.lock"
    if not path.exists():
        return False
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(descriptor)


def status_lines(root: Path, config: dict, principal_override=None):
    runtime_path = root / ".ralph/runtime.json"
    runtime = read_object(runtime_path) if runtime_path.exists() else {}
    for role in ("worker", "reviewer", "manager"):
        principal = principal_override or config["roles"][role]["agentName"]
        state = runtime.get("agents", {}).get(principal, {}).get(role, {})
        path = root / ".ralph/github-state" / principal / f"{role}.json"
        execution = read_object(path) if path.exists() else {}
        records = list(deferred_records(root, role))
        own = [(p, r) for p, r in records if r.get("principal") == principal]
        running = runner_alive(root, principal, role)
        phase = state.get("activity", {}).get("phase")
        if not running:
            label = "Runner停止"
        elif state.get("paused"):
            label = "paused（停止設定）"
        elif phase == "retry_wait":
            label = "再試行待ち"
        elif phase == "agent_running":
            label = "復旧Agent実行中" if execution.get("operationRecovery") else "Agent実行中"
            try:
                os.kill(int(state["activity"]["pid"]), 0)
            except (ProcessLookupError, ValueError, KeyError):
                label = "Agentプロセスなし・結果確認待ち"
            except PermissionError:
                label = "Agentプロセスの確認権限なし"
        elif execution:
            label = {"applying": "結果反映中", "agent_finished": "Agent終了・結果確認中"}.get(phase, "実行記録あり・準備/再試行待ち")
        elif own:
            label = "待機（保留Taskあり）"
        else:
            label = "待機（実行中Taskなし）"
        yield f"{role}:{principal} {label} paused={str(bool(state.get('paused'))).lower()} 保留={len(own)}"
        if running and phase == "agent_running":
            yield f"  Agent PID={state['activity'].get('pid', '')} 経過={max(0, int(time.time()) - state['activity'].get('updatedAt', int(time.time())))}秒"
        if execution:
            yield f"  Task={execution.get('task', {}).get('title', '')} [{execution.get('task', {}).get('id', '')}]"
            yield f"  記録={path}"
        if phase == "retry_wait" and running:
            yield "  再試行理由=" + brief_reason(state.get("activity", {}).get("reason", ""))
        if state.get("paused"):
            yield "  理由=" + brief_reason(state.get("reason", ""))
        for path, record in own:
            yield f"  保留={record['task'].get('title', '')} [{record['task']['id']}]"
            yield "  理由=" + brief_reason(record.get("deferredReason", ""))
            yield "  記録=" + str(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--role", choices=("worker", "reviewer", "manager"), required=True)
    args = parser.parse_args()
    config = read_object(args.config)
    root = project_root(args.config, config)
    summary = json.load(sys.stdin)
    own = [record for _, record in deferred_records(root, args.role)
        if record.get("principal") == principal_for(config, args.role)
        and record.get("projectId") == summary.get("projectId") and can_recheck(record)]
    accepted = []
    if own:
        if __package__:
            from .github_workflow import Workflow
        else:
            from github_workflow import Workflow
        accepted = accepted_tasks(Workflow(args.config, args.role), own)
    print(json.dumps(filter_summary(root, config, args.role, summary, accepted), ensure_ascii=False))


if __name__ == "__main__":
    main()
