#!/usr/bin/env python3
"""Local stdio MCP bridge for Project-approved host verification commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from .github_workflow import Workflow, WorkflowError, atomic_json, run
else:
    from github_workflow import Workflow, WorkflowError, atomic_json, run

TIMEOUT_SECONDS = 3600
SAFE_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR"}


def commands(config: dict) -> list[list[str]]:
    section = config.get("verification", {})
    if not isinstance(section, dict):
        raise WorkflowError("verification must be an object")
    value = section.get("hostCommands", [])
    if not isinstance(value, list) or any(not isinstance(item, list) or not item or
            any(not isinstance(arg, str) or not arg or "\0" in arg for arg in item) for item in value):
        raise WorkflowError("verification.hostCommands must be an array of non-empty argument arrays")
    return value


def fingerprint(cwd: Path) -> str:
    digest = hashlib.sha256(subprocess.check_output(("git", "diff", "--binary", "HEAD"), cwd=cwd))
    files = subprocess.check_output(("git", "ls-files", "--others", "--exclude-standard", "-z"), cwd=cwd).decode().split("\0")
    for name in sorted(files):
        if not name or name.startswith(".ralph/") or name == ".ralph-result.json":
            continue
        path = cwd / name
        digest.update(name.encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        else:
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(65536), b""):
                    digest.update(chunk)
    return digest.hexdigest()


class VerificationServer:
    def __init__(self, config: Path, role: str, claim_id: str):
        self.config, self.role, self.claim_id = config, role, claim_id
        self.last_record: Path | None = None

    def execute(self, arguments: dict) -> dict:
        self.last_record = None
        if not isinstance(arguments, dict):
            raise WorkflowError("Verification arguments must be an object")
        workflow = Workflow(self.config, self.role)  # Always reload the host-owned configuration.
        command = arguments.get("command")
        if set(arguments) != {"command"} or command not in commands(workflow.config):
            raise WorkflowError("Command and arguments must exactly match verification.hostCommands")
        state = workflow.state
        if not state or state.get("claimId") != self.claim_id or state.get("completed"):
            raise WorkflowError("Verification is not attached to the active execution")
        cwd = Path(state["cwd"])
        expected = workflow.root / ".ralph/worktrees" / f"{self.role}-{state['task']['id']}-{state.get('worktreeClaimId', self.claim_id)[:8]}"
        if cwd != expected or cwd.resolve() != expected.absolute():
            raise WorkflowError("Verification cwd must be the registered Task worktree")
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=workflow.root).split("\0")
        if f"worktree {cwd}" not in registered:
            raise WorkflowError("Verification cwd is not a registered worktree")
        workflow.renew()  # Never reacquire a different Claim while an Agent is running.
        directory = workflow.root / ".ralph/verification" / self.claim_id
        directory.mkdir(parents=True, exist_ok=True)
        record_path = directory / f"{uuid.uuid4().hex}.json"
        self.last_record = record_path
        log_path = record_path.with_suffix(".log")
        record = {"taskId": state["task"]["id"], "claimId": self.claim_id, "role": self.role,
            "cwd": str(cwd), "command": command, "headSha": run("git", "rev-parse", "HEAD", cwd=cwd),
            "sourceFingerprint": fingerprint(cwd), "logPath": str(log_path), "startedAt": int(time.time() * 1000),
            "status": "running", "exitCode": None}
        atomic_json(record_path, record)
        process = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=cwd, env={k: v for k, v in os.environ.items() if k in SAFE_ENV},
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + TIMEOUT_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, TIMEOUT_SECONDS)
                    try:
                        record["exitCode"] = process.wait(timeout=min(60, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise
                        workflow.renew()
            record["status"] = "passed" if record["exitCode"] == 0 else "failed"
            record["sourceUnchanged"] = record["sourceFingerprint"] == fingerprint(cwd)
            if run("git", "rev-parse", "HEAD", cwd=cwd) != record["headSha"]:
                record["sourceUnchanged"] = False
        except BaseException as error:
            record["status"] = "timeout" if isinstance(error, subprocess.TimeoutExpired) else "interrupted"
            if isinstance(error, OSError):
                record["status"] = "launch_failed"
            raise
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            record["finishedAt"] = int(time.time() * 1000)
            atomic_json(record_path, record)
        return record

    def handle(self, request: dict) -> dict | None:
        if "id" not in request:
            return None
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ralph-verification", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            allowed = commands(Workflow(self.config, self.role).config)
            if not allowed:
                return {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": []}}
            result = {"tools": [{"name": "run_host_verification", "description":
                "Run one Project-approved verification command on the host in the active Task worktree. "
                "Read its log and verify headSha/sourceFingerprint before using it as evidence. "
                "Commands are matched exactly; this executes outside the Agent sandbox.",
                "inputSchema": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}, "enum": allowed}},
                    "required": ["command"], "additionalProperties": False}}]}
        elif method == "tools/call":
            params = request.get("params", {})
            try:
                if params.get("name") != "run_host_verification":
                    raise WorkflowError("Unknown verification tool")
                result = self.execute(params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result, "isError": result["status"] != "passed"}
            except (WorkflowError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
                message = str(error) if isinstance(error, WorkflowError) else "Host verification interrupted; inspect verification records."
                result = {"content": [{"type": "text", "text": message}], "isError": True}
                if self.last_record and self.last_record.exists():
                    result["structuredContent"] = json.loads(self.last_record.read_text())
        else:
            return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--role", choices=["worker", "reviewer", "manager"], required=True)
    parser.add_argument("--claim-id", required=True)
    args = parser.parse_args()
    server = VerificationServer(args.config, args.role, args.claim_id)
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    for line in sys.stdin:
        response = server.handle(json.loads(line))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
