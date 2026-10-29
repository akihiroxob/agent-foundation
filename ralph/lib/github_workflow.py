#!/usr/bin/env python3
"""Trusted runner for Git/PR mutations; agents only produce a decision file."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class WorkflowError(Exception):
    pass


class RetryLater(WorkflowError):
    pass


class WorkerRepairNeeded(WorkflowError):
    def __init__(self, code: str, reason: str, diagnostics: list | None = None):
        super().__init__(reason)
        self.code = code
        self.diagnostics = diagnostics or []


class WorkerRepairScheduled(WorkflowError):
    pass


def run(*args: str, cwd: Path, env: dict | None = None) -> str:
    for attempt in range(3):
        result = subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {}), "LC_ALL": "C"},
                                capture_output=True, text=True, check=False)
        if not result.returncode:
            return result.stdout.strip()
        contention = args[0] == "git" and ("cannot lock ref" in result.stderr or
                     (".lock" in result.stderr and "File exists" in result.stderr))
        if contention:
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise RetryLater("Git shared metadata is temporarily locked; retry without deleting lock files")
        operation = next((item for item in args[1:] if item in {
            "fetch", "push", "commit", "worktree", "diff", "status", "merge-base", "rev-parse", "config", "ls-remote"}), args[1])
        # Raw stderr/arguments can contain credentials. Classify only known safe facts.
        category = "command failed"
        for patterns, description in [
            (("Authentication failed", "could not read Username"), "authentication failed; check the configured PAT"),
            (("Could not resolve host", "Failed to connect", "Connection timed out"), "network connection failed"),
            (("Permission denied", "Operation not permitted"), "filesystem/OS permission denied"),
            (("non-fast-forward", "fetch first"), "remote branch advanced; review/reconcile the branch"),
        ]:
            if any(pattern in result.stderr for pattern in patterns):
                category = description
                break
        raise WorkflowError(f"{args[0]} {operation} failed (exit {result.returncode}): {category}")



if __package__:
    from .runtime_state import read_object, atomic_json, execution_path, principal_for, project_root, worktree_path
else:
    from runtime_state import read_object, atomic_json, execution_path, principal_for, project_root, worktree_path


class RemoteError(WorkflowError):
    def __init__(self, status: int, operation: str):
        self.status = status
        self.operation = operation
        super().__init__(f"GitHub {operation}: HTTP {status}")


class ReviewRepairScheduled(WorkflowError):
    pass


if __name__ == "__main__":
    sys.modules["github_workflow"] = sys.modules[__name__]


if __package__:
    from .operation_recovery import OperationRecovery, RecoveryDeferred
    from .execution_status import brief_reason
else:
    from operation_recovery import OperationRecovery, RecoveryDeferred
    from execution_status import brief_reason


class Workflow(OperationRecovery):
    def __init__(self, config_path: Path, role: str, principal: str | None = None):
        self.config_path = config_path.resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.role = role
        self.root = project_root(self.config_path, self.config)
        self.principal = principal if principal is not None else principal_for(self.config, role)
        self.state_path = execution_path(self.root, self.principal, role)
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        if self.state and self.state.get("principal", self.principal) != self.principal:
            raise WorkflowError("Execution Principal does not match the selected Principal")
        names = self.token_names()
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*_TOKEN", name) for name in names.values()):
            raise WorkflowError("github.tokenEnvs values must be dedicated *_TOKEN environment variable names")
        self.workflow: dict = {}
        self.phase = "initialization"

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def host_commands(self) -> list[list[str]]:
        if __package__:
            from .host_verification import commands
        else:
            from host_verification import commands
        if __package__:
            from .preparation_workspace import preparation_files
        else:
            from preparation_workspace import preparation_files
        preparation_files(self.config)
        return commands(self.config)

    def agent_mcp_config(self, claim_id: str) -> dict:
        if __package__:
            from .mcp_config import read_servers
        else:
            from mcp_config import read_servers
        try:
            servers = read_servers(self.root / ".mcp.json")
        except ValueError as error:
            raise WorkflowError(str(error)) from error
        servers["wacha"] = {"type": "http", "url": self.config["wacha"]["url"],
            "headers": {"Authorization": "Bearer ${WACHA_AGENT_NAME}"}}
        if __package__:
            from .browser_preview import browser_config, playwright_config
        else:
            from browser_preview import browser_config, playwright_config
        browser = browser_config(self.config) if self.role == "manager" else None
        if browser:
            servers["playwright"] = playwright_config(self.root)
            output = self.root / ".ralph/verification" / claim_id / "browser"
            output.mkdir(parents=True, exist_ok=True)
            servers["playwright"]["args"] = [*servers["playwright"]["args"], "--output-dir", str(output)]
            servers["playwright"]["timeout"] = 180000
        if self.host_commands() or browser:
            servers["ralph_verification"] = {"command": sys.executable, "args": [
                str(Path(__file__).with_name("host_verification.py")), "--config", str(self.config_path),
                "--role", self.role, "--claim-id", claim_id, "--principal", self.principal], "timeout": 3660000}
        return {"mcpServers": servers}

    def request(self, url: str, token: str, method: str = "GET", body: dict | None = None) -> dict | list:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # No response bodies or tokens in logs.
            if error.code >= 500 or error.code == 429 or error.headers.get("x-ratelimit-remaining") == "0" or error.headers.get("retry-after"):
                raise RetryLater(f"Remote service returned HTTP {error.code}") from error
            raise RemoteError(error.code, method + " " + urllib.parse.urlparse(url).path) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RetryLater("Remote service unavailable") from error

    def mcp(self, name: str, arguments: dict) -> dict:
        self.phase = "wacha:" + name
        url = self.config["wacha"]["url"]
        request = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}).encode(), headers={
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.principal}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                values = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
                value = next(item for item in reversed(values) if item.get("id") == 1)
            result = value.get("result", {})
            content = result.get("structuredContent")
            if value.get("error") or result.get("isError") or not isinstance(content, dict):
                code = (content or {}).get("error", {}).get("code", "MCP_ERROR")
                if code in {"GITHUB_UNAVAILABLE", "CLAIM_CONFLICT"}:
                    raise RetryLater(f"Wacha {name}: {code}")
                raise WorkflowError(f"Wacha {name}: {code}")
            return content
        except (urllib.error.URLError, TimeoutError) as error:
            raise RetryLater("Wacha unavailable") from error

    def command(self, name: str, extra: dict | None = None) -> dict:
        arguments = {"taskId": self.state["task"]["id"], "claimId": self.state["claimId"],
            "requestId": f"ralph:{self.state['claimId']}:{name}"}
        if name == "release_claim":
            arguments.pop("taskId")
        arguments.update(extra or {})
        return self.mcp(name, arguments)

    def token_names(self) -> dict:
        return self.config.get("github", {}).get("tokenEnvs", {
            "worker": "RALPH_GITHUB_TOKEN", "reviewer": "RALPH_GITHUB_TOKEN",
            "manager": "RALPH_GITHUB_TOKEN"})

    def github_token(self) -> str:
        name = self.token_names().get(self.role)
        token = os.environ.get(name or "")
        if not token:
            raise WorkflowError(f"GitHub credential environment variable {name} is missing")
        return token

    def github(self, path: str, method: str = "GET", body: dict | None = None) -> dict | list:
        self.phase = "github:" + method + " " + path
        return self.request(f"https://api.github.com{path}", self.github_token(), method, body)

    def git_remote(self, operation: str, refspec: str, cwd: Path) -> str:
        self.phase = "git:" + operation
        if operation not in {"fetch", "push"}:
            raise WorkflowError("Unsupported remote Git operation")
        self.validate_remote()
        repository = self.workflow["repository"]
        url = f"https://github.com/{repository}.git"
        # --get-url performs no network operation. Never send the token through
        # a configured insteadOf rewrite to another host or back to SSH.
        if run("git", "ls-remote", "--get-url", url, cwd=cwd) != url:
            raise WorkflowError("Git URL rewrite changes the expected HTTPS repository")
        if operation == "push":
            rewrites = subprocess.run(("git", "config", "--null", "--get-regexp", r"^url\..*\.pushinsteadof$"),
                cwd=cwd, capture_output=True, text=True, check=False)
            if rewrites.returncode not in {0, 1}:
                raise WorkflowError("Cannot inspect Git push URL rewrites")
            if any(url.startswith(item.partition("\n")[2]) for item in rewrites.stdout.split("\0") if item):
                raise WorkflowError("Git push URL rewrite changes the expected HTTPS repository")
        environment = os.environ.copy()
        for name in list(environment):
            if name.startswith("GIT_TRACE") or name == "GIT_CURL_VERBOSE":
                environment.pop(name)
        environment.update({"RALPH_GIT_AUTH_TOKEN": self.github_token(),
            "RALPH_GIT_REPOSITORY": repository, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
        helper = "!" + shlex.quote(sys.executable) + " " + shlex.quote(str(Path(__file__).with_name("git_credential.py")))
        options = ("-c", "credential.helper=", "-c", f"credential.helper={helper}",
            "-c", "credential.useHttpPath=true", "-c", "http.extraHeader=",
            "-c", f"http.{url}.extraHeader=", "-c", "http.followRedirects=false")
        return run("git", *options, operation, url, refspec, cwd=cwd, env=environment)

    def pages(self, path: str, key: str | None = None) -> list:
        result = []
        for page in range(1, 21):
            value = self.github(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            values = value[key] if key else value
            if not isinstance(values, list):
                raise WorkflowError("Invalid GitHub paginated response")
            result.extend(values)
            if len(values) < 100:
                return result
        raise WorkflowError("GitHub pagination limit reached")

    def repo_path(self) -> str:
        return f"/repos/{self.workflow['repository']}"

    def validate_remote(self) -> None:
        remote = run("git", "remote", "get-url", "origin", cwd=self.root)
        match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", remote)
        if not match or match[1].lower() != self.workflow["repository"].lower():
            raise WorkflowError("origin must match the Wacha Project GitHub repository")

    def prepare(self) -> dict:
        self.retry_completed_cleanup()
        if self.state.get("restorePending"):
            self.finish_restore()
        if self.state.get("deferred"):
            self.finish_deferral()
            return {"idle": True}
        if self.state.get("recoveryBudget") and not self.recovery_remaining():
            self.defer_execution("Recovery elapsed time limit reached")
            raise RecoveryDeferred("Recovery elapsed time limit reached")
        if self.state.get("operationRecovery"):
            return self.recovery_context()
        if not self.state:
            self.restore_deferred()
        self.host_commands()  # Reject invalid host configuration before acquiring a Claim.
        if self.state.get("completed"):
            return {**self.state, "pending": True}
        projects = self.mcp("list_projects", {})["projects"]
        project = next((item for item in projects if item["name"] == self.config["projectName"]), None)
        if not project or not project.get("workflow"):
            raise WorkflowError("Enable GitHub workflow in Wacha Project settings first")
        self.workflow = project["workflow"]
        branch = self.workflow["baseBranch"]
        if branch.startswith("-"):
            raise WorkflowError("Invalid base branch")
        run("git", "check-ref-format", f"refs/heads/{branch}", cwd=self.root)
        if self.state:
            if self.state.get("pendingResult"):
                if not self.state.get("finalCommand"):
                    self.renew_or_reclaim()
                return {**self.state, "pending": True, "workflow": self.workflow}
            self.renew_or_reclaim()
        else:
            self.validate_remote()
            if self.role != "manager":
                self.github("/user")  # Fail before claiming when credentials are absent.
            availability = {"worker": "work", "reviewer": "review", "manager": "acceptance"}[self.role]
            tasks = self.mcp("list_tasks", {"projectId": project["id"], "filter": {"availableFor": availability}})["tasks"]
            deferred = {(record["projectId"], record["task"]["id"]) for _, record in self.deferred_records()}
            tasks = [task for task in tasks if (project["id"], task["id"]) not in deferred]
            if self.role == "manager":
                tasks = [task for task in tasks if task["status"] == "wait_accept"]
            if self.role == "worker":
                tasks.sort(key=lambda task: task["status"] != "rejected")
            if not tasks:
                return {"idle": True}
            command = {"worker": "claim_task", "reviewer": "claim_review", "manager": "claim_acceptance"}[self.role]
            # Each race loser tries another candidate; never treat contention as a role fault.
            task = None
            for candidate in tasks:
                try:
                    claim = self.mcp(command, {"taskId": candidate["id"], "requestId": f"ralph:claim:{self.role}:{os.urandom(16).hex()}"})
                    task = candidate
                    break
                except RetryLater as error:
                    if "CLAIM_CONFLICT" not in str(error):
                        raise
            if task is None:
                return {"idle": True}
            self.state = {"task": task, "claimId": claim["claimId"], "projectId": project["id"],
                          "principal": self.principal, "role": self.role}
            self.save()
        history = self.mcp("list_task_pull_requests", {"taskId": self.state["task"]["id"]})["pullRequests"]
        self.state["pullRequests"] = history
        branch = self.workflow["baseBranch"]
        self.git_remote("fetch", f"refs/heads/{branch}:refs/remotes/origin/{branch}", self.root)
        if self.state["task"].get("completionMode") == "evidence":
            if history:
                raise WorkflowError("Evidence Task unexpectedly has PR history; retain execution")
            start = run("git", "rev-parse", f"origin/{branch}", cwd=self.root)
            if self.role == "worker":
                self.state.setdefault("branch", f"ralph/{self.state['task']['id']}/{self.state['claimId'][:8]}")
            elif self.role == "manager":
                self.state["expectedMergeSha"] = start
            else:
                self.state["expectedHeadSha"] = start
        elif self.role == "worker":
            if history and history[-1]["state"] == "open":
                pr = self.github(f"{self.repo_path()}/pulls/{history[-1]['number']}")
                if pr["state"] == "open":
                    if pr["head"]["repo"]["full_name"].lower() != self.workflow["repository"].lower():
                        raise WorkflowError("Ralph cannot push to a fork PR")
                    self.state["branch"] = pr["head"]["ref"]
                    self.git_remote("fetch", f"refs/pull/{pr['number']}/head", self.root)
                    start = pr["head"]["sha"]
                else:
                    start = f"origin/{branch}"
            else:
                start = f"origin/{branch}"
            self.state.setdefault("branch", f"ralph/{self.state['task']['id']}/{self.state['claimId'][:8]}")
        else:
            if not history:
                raise WorkflowError("Task has no registered PR")
            pr = history[-1]
            if self.role == "reviewer":
                remote_pr = self.github(f"{self.repo_path()}/pulls/{pr['number']}")
                self.git_remote("fetch", f"refs/pull/{pr['number']}/head", self.root)
                start = remote_pr["head"]["sha"]
                self.state["expectedHeadSha"] = start
            else:
                start = pr["mergeSha"]
                if not start or not pr["reviewedHeadSha"]:
                    raise WorkflowError("Acceptance requires a reviewed merged PR")
                run("git", "merge-base", "--is-ancestor", start, f"origin/{branch}", cwd=self.root)
                self.state["expectedMergeSha"] = start
        worktree_claim = self.state.setdefault("worktreeClaimId", self.state["claimId"])
        directory = self.expected_worktree(self.state)
        if directory.exists() and not self.state.get("cwd"):
            raise WorkflowError("Worktree already exists without this execution ownership; existing files retained")
        self.state["cwd"] = str(directory)
        self.save()
        if not directory.exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            arguments = ["git", "worktree", "add"]
            if self.role == "worker":
                arguments += ["-b", f"ralph/local/{self.state['task']['id']}/{self.state['claimId'][:8]}"]
            else:
                arguments += ["--detach"]
            run(*arguments, str(directory), start, cwd=self.root)
        elif self.role != "worker":
            # Never silently reuse a review checkout after the PR head changed.
            if run("git", "rev-parse", "HEAD", cwd=directory) != start:
                raise WorkflowError("Review checkout SHA changed; release the Claim and prepare a fresh worktree")
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if (directory.resolve() != directory.absolute() or directory.is_symlink()
                or f"worktree {directory}" not in registered):
            raise WorkflowError("Execution cwd is not the expected registered Worktree; existing files retained")
        if not self.state.get("setupDone"):
            commands = self.config.get("github", {}).get("setupCommands", [])
            if not isinstance(commands, list) or any(not isinstance(command, list) or not command or
                    any(not isinstance(arg, str) for arg in command) for command in commands):
                raise WorkflowError("github.setupCommands must be an array of argument arrays")
            for command in commands:
                run(*command, cwd=directory)
            self.state["setupDone"] = True
        self.state["cwd"] = str(directory)
        if __package__:
            from .preparation_workspace import ensure_preparation
        else:
            from preparation_workspace import ensure_preparation
        preparation = ensure_preparation(self)
        if preparation is not None:
            self.state["verificationPreparation"] = preparation
        self.state["resultPath"] = str(directory / ".ralph-result.json")
        self.state.setdefault("initialHeadSha", run("git", "rev-parse", "HEAD", cwd=directory))
        # Keep the trusted Project definitions and Claim-bound additions in the execution worktree.
        self.state["mcpClaimId"] = self.state["claimId"]
        self.state["agentMcpConfig"] = self.agent_mcp_config(self.state["mcpClaimId"])
        atomic_json(directory / ".ralph" / "mcp.json", self.state["agentMcpConfig"])
        result_path = Path(self.state["resultPath"])
        result_path.unlink(missing_ok=True)
        self.save()
        if __package__:
            from .browser_preview import browser_config
        else:
            from browser_preview import browser_config
        browser_enabled = self.role == "manager" and browser_config(self.config) is not None
        skill = Path(__file__).resolve().parents[1] / "skills/verify-browser-ac/SKILL.md"
        if not skill.exists():
            skill = Path(__file__).resolve().parents[2] / ".apm/skills/verify-browser-ac/SKILL.md"
        return {**self.state, "workflow": self.workflow, "hostVerificationCommands": self.host_commands(),
            "browserVerificationEnabled": browser_enabled,
            "browserVerificationSkill": str(skill) if browser_enabled else None,
            "mcpUsageSkill": str(skill.parent.parent / "use-mcp-tools/SKILL.md"),
            "recoveryTimeoutSeconds": self.recovery_remaining() if self.state.get("recoveryBudget") else 0}

    def renew(self) -> None:
        if self.state:
            # renew_claim has no taskId argument.
            self.mcp("renew_claim", {"claimId": self.state["claimId"]})

    def reacquire_released_execution(self) -> None:
        # Explicit resume starts a new Role judgment, preserving the old blocked evidence.
        intent = self.state["releasedResume"]
        statuses = {"worker": ["todo", "rejected"], "reviewer": ["in_review"], "manager": ["wait_accept"]}[self.role]
        availability = {"worker": "work", "reviewer": "review", "manager": "acceptance"}[self.role]
        query = {"status": [*statuses, "doing"]} if intent.get("requestId") else {"availableFor": availability}
        tasks = self.mcp("list_tasks", {"projectId": self.state["projectId"], "filter": query})["tasks"]
        candidate = next((task for task in tasks if task["id"] == self.state["task"]["id"]), None)
        allowed = statuses + (["doing"] if self.role == "worker" and intent.get("requestId") else [])
        if not candidate or candidate["status"] not in allowed:
            raise WorkflowError("Released execution cannot resume: Task is unavailable or its phase changed; evidence retained")
        if any(candidate.get(key) != self.state["task"].get(key) for key in ("title", "description", "storyId")):
            raise WorkflowError("Released Task requirements changed; evidence retained for reassessment")
        if not intent.get("requestId"):
            intent["requestId"] = f"ralph:resume:{self.role}:{os.urandom(16).hex()}"
            self.save()
        operation = {"worker": "claim_task", "reviewer": "claim_review", "manager": "claim_acceptance"}[self.role]
        claim = self.mcp(operation, {"taskId": candidate["id"], "requestId": intent["requestId"]})
        old = self.state["claimId"]
        if claim["claimId"] != old:
            self.state.setdefault("previousClaimIds", []).append(old)
        self.state["claimId"] = claim["claimId"]
        self.state["task"] = candidate
        self.state.pop("releasedResume")
        self.state.pop("releasedClaim", None)
        self.state.pop("reviewCommentId", None)
        self.save()

    def renew_or_reclaim(self) -> None:
        if not self.state:
            return
        if "releasedResume" in self.state:
            self.reacquire_released_execution()
            return
        expected = {"worker": "doing", "reviewer": "in_review", "manager": "wait_accept"}[self.role]
        if self.state.get("claimRecovery"):
            # A previous request may have succeeded without returning its response.
            # Check requirements again, but don't reject our own active Claim;
            # replaying the saved requestId retrieves its receipt.
            tasks = self.mcp("list_tasks", {"projectId": self.state["projectId"],
                "filter": {"status": [expected]}})["tasks"]
            candidate = next((task for task in tasks if task["id"] == self.state["task"]["id"]), None)
            if not candidate or any(candidate.get(key) != self.state["task"].get(key) for key in ("title", "description", "storyId")):
                raise WorkflowError("Task phase or requirements changed during Claim recovery; execution retained")
        if not self.state.get("claimRecovery"):
            try:
                self.renew()
                return
            except WorkflowError as error:
                if "CLAIM_EXPIRED" not in str(error):
                    raise
            availability = {"worker": "work", "reviewer": "review", "manager": "acceptance"}[self.role]
            tasks = self.mcp("list_tasks", {"projectId": self.state["projectId"],
                "filter": {"availableFor": availability}})["tasks"]
            candidate = next((task for task in tasks if task["id"] == self.state["task"]["id"]), None)
            if not candidate or candidate["status"] != expected or (self.role == "worker" and not candidate.get("reclaimable")):
                raise WorkflowError("Expired Claim cannot be reacquired: Task is unavailable or its phase changed; execution retained")
            if any(candidate.get(key) != self.state["task"].get(key) for key in ("title", "description", "storyId")):
                raise WorkflowError("Task requirements changed after Claim expiry; execution retained for operator review")
            self.state["claimRecovery"] = {"requestId": f"ralph:reclaim:{self.role}:{os.urandom(16).hex()}"}
            self.save()  # Retry a lost response with the same requestId.
        operation = {"worker": "claim_task", "reviewer": "claim_review", "manager": "claim_acceptance"}[self.role]
        claim = self.mcp(operation, {"taskId": self.state["task"]["id"],
            "requestId": self.state["claimRecovery"]["requestId"]})
        old_claim = self.state["claimId"]
        self.state.setdefault("worktreeClaimId", old_claim)
        self.state.setdefault("previousClaimIds", []).append(old_claim)
        self.state["claimId"] = claim["claimId"]
        self.state.pop("claimRecovery", None)
        self.state.pop("reviewCommentId", None)  # Evidence is bound to the Review Claim.
        self.state.pop("finalCommand", None)  # Revalidate and submit under the new Claim.
        self.save()
        self.renew()

    def note(self, text: str) -> None:
        self.command("add_task_comment", {"body": text})

    def transition(self, name: str, extra: dict | None = None) -> None:
        self.state["finalCommand"] = {"name": name, "extra": extra}
        self.save()
        self.command(name, extra)

    def clear(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def expected_worktree(self, execution: dict) -> Path:
        if execution.get("legacyWorktree"):
            claim = execution.get("worktreeClaimId", execution["claimId"])
            return self.root / ".ralph/worktrees" / f"{self.role}-{execution['task']['id']}-{claim[:8]}"
        return worktree_path(self.root, self.principal, self.role)

    def cleanup_worktree(self, execution: dict) -> str:
        """Remove verified completed/released checkouts; retain product changes and unknown files."""
        if (self.state and self.state.get("claimId") != execution.get("claimId")
                and self.state.get("cwd") == execution.get("cwd")):
            raise WorkflowError("Worktree is attached to another active Claim; cleanup refused")
        operations = {"worker": {"complete_task"}, "reviewer": {"reviewed_task", "reject_task"},
                      "manager": {"accept_task", "reject_task"}}
        completed = execution.get("completed") and execution.get("finalCommand", {}).get("name") in operations[self.role]
        released = execution.get("releasedClaim") and execution.get("retainedReason")
        if not completed and not released:
            raise WorkflowError("Execution is not successfully completed or released")
        directory = Path(execution["cwd"])
        expected = self.expected_worktree(execution)
        if directory != expected or directory.resolve() != expected.absolute():
            raise WorkflowError("Worktree path is not the expected Ralph checkout")
        if not directory.exists():
            return "removed"
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if f"worktree {directory}" not in registered:
            raise WorkflowError("Checkout is not a registered Ralph worktree")
        sha = execution.get({"worker": "commitSha", "reviewer": "expectedHeadSha", "manager": "expectedMergeSha"}[self.role])
        if self.role == "worker" and (released or execution.get("task", {}).get("completionMode") == "evidence") and not sha:
            sha = execution.get("initialHeadSha")
        if not sha or run("git", "rev-parse", "HEAD", cwd=directory) != sha:
            raise WorkflowError("Worktree contains an unexpected commit")
        if run("git", "status", "--porcelain", "--untracked-files=no", cwd=directory):
            raise WorkflowError("Worktree contains uncommitted changes")
        runtime_paths = {".ralph/mcp.json", ".ralph-result.json"}
        untracked = set(run("git", "ls-files", "--others", "--exclude-standard", "-z", cwd=directory).split("\0")) - {""}
        runtime_cache = ".ralph/tmp/node-compile-cache"
        unknown = {name for name in untracked - runtime_paths if not name.startswith(runtime_cache + "/")}
        if unknown:
            raise WorkflowError("Worktree contains untracked files")
        tracked = set(run("git", "ls-files", "-z", cwd=directory).split("\0"))
        if (tracked & runtime_paths or any(name == runtime_cache or name.startswith(runtime_cache + "/") for name in tracked)
                or (directory / ".ralph").is_symlink()):
            raise WorkflowError("Runner files are tracked or redirected")
        # git worktree remove refuses untracked runner files, so remove only the
        # two known JSON artifacts after persisting their result outside the checkout.
        for relative in runtime_paths:
            path = directory / relative
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise WorkflowError("Runner artifact is not a regular file")
            if path.exists():
                expected_json = execution.get("pendingResult") if relative == ".ralph-result.json" else execution.get("agentMcpConfig", {
                    "mcpServers": {"wacha": {"type": "http", "url": self.config["wacha"]["url"],
                        "headers": {"Authorization": "Bearer ${WACHA_AGENT_NAME}"}}}})
                try:
                    actual_json = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError) as error:
                    raise WorkflowError("Runner artifact contains unexpected content") from error
                if actual_json != expected_json:
                    raise WorkflowError("Runner artifact changed after completion")
        cache = directory / runtime_cache
        for relative in (".ralph/tmp", runtime_cache):
            path = directory / relative
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise WorkflowError("Runtime cache path is redirected or not a directory")
        if cache.exists():
            if any(path.is_symlink() for path in cache.rglob("*")):
                raise WorkflowError("Runtime cache contains redirected paths")
            shutil.rmtree(cache)
        for relative in runtime_paths:
            (directory / relative).unlink(missing_ok=True)
        run("git", "worktree", "remove", str(directory), cwd=self.root)
        return "removed"

    def cleanup_local_branch(self, execution: dict) -> str:
        """Delete only the completed Worker's exact, remotely preserved local ref."""
        if self.role != "worker":
            return "not_applicable"
        if not execution.get("completed") or execution.get("finalCommand", {}).get("name") != "complete_task":
            return "retained"
        if execution.get("cleanup") != "removed":
            return "retained"
        claim = execution.get("worktreeClaimId", execution["claimId"])
        branch = f"ralph/local/{execution['task']['id']}/{claim[:8]}"
        ref = "refs/heads/" + branch
        run("git", "check-ref-format", ref, cwd=self.root)
        found = run("git", "for-each-ref", "--format=%(objectname)", ref, cwd=self.root).splitlines()
        if not found:
            return "removed"
        expected = execution.get("commitSha") or execution.get("initialHeadSha")
        if found != [expected]:
            raise WorkflowError("Local branch contains an unexpected commit: " + branch)
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if "branch " + ref in registered:
            raise WorkflowError("Local branch is used by a Worktree: " + branch)
        if not run("git", "for-each-ref", "--contains", expected, "--format=%(refname)", "refs/remotes/origin/", cwd=self.root):
            raise WorkflowError("Local branch commit has no remote tracking reference; retained: " + branch)
        # Compare-and-delete prevents removing a branch advanced after the checks.
        run("git", "update-ref", "-d", ref, expected, cwd=self.root)
        return "removed"

    def cleanup_record(self, path: Path, execution: dict) -> None:
        try:
            if execution.get("cleanup") != "removed":
                execution["cleanup"] = self.cleanup_worktree(execution)
            execution.pop("cleanupError", None)
        except (WorkflowError, OSError) as error:
            execution["cleanup"] = "retained"
            execution["cleanupError"] = str(error)
            print(f"{self.role}: completed worktree retained; {error}", file=sys.stderr)
        try:
            execution["branchCleanup"] = self.cleanup_local_branch(execution)
            execution.pop("branchCleanupError", None)
        except (WorkflowError, OSError) as error:
            execution["branchCleanup"] = "retained"
            execution["branchCleanupError"] = str(error)
            print(f"{self.role}: local branch retained: {error}", file=sys.stderr)
        atomic_json(path, execution)

    def retry_completed_cleanup(self) -> None:
        for path in (self.state_path.parent / "completed").glob(f"{self.role}-*.json"):
            try:
                execution = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(execution, dict):
                    raise ValueError("Completed record must be an object")
            except (ValueError, OSError) as error:
                print(f"{self.role}: cannot read cleanup record {path.name} ({type(error).__name__}); retained", file=sys.stderr)
                continue
            if execution.get("completed") and (execution.get("cleanup") != "removed" or execution.get("branchCleanup") not in {"removed", "not_applicable"}):
                self.cleanup_record(path, execution)

    def complete_execution(self) -> None:
        # Persist success before cleanup so a crash never renews a completed Claim
        # or reruns the Agent. The final result remains available after removal.
        self.state["completed"] = True
        self.save()
        decision = self.state.get("pendingResult", {}).get("decision", "saved")
        title = re.sub(r"\s+", " ", self.state.get("task", {}).get("title", ""))
        print(f"Task反映完了 decision={decision} Task={title} [{self.state.get('task', {}).get('id', '')}]", flush=True)
        archive = self.state_path.parent / "completed" / f"{self.role}-{self.state['claimId']}.json"
        atomic_json(archive, self.state)
        self.cleanup_record(archive, self.state)
        self.clear()

    def finish(self) -> None:
        if self.state.get("recoveryBudget") and not self.recovery_remaining():
            self.defer_execution("Recovery elapsed time limit reached")
            raise RecoveryDeferred("Recovery elapsed time limit reached")
        if not self.state:
            raise WorkflowError("No active GitHub execution")
        if self.state.get("completed"):
            self.complete_execution()
            return
        if self.state.get("finalCommand"):
            operation = self.state["finalCommand"]
            try:
                self.command(operation["name"], operation["extra"])
                self.complete_execution()
                return
            except WorkflowError as error:
                if "CLAIM_EXPIRED" not in str(error):
                    raise
        project = next(item for item in self.mcp("list_projects", {})["projects"] if item["id"] == self.state["projectId"])
        self.workflow = project["workflow"]
        self.renew_or_reclaim()
        self.phase = "result:read"
        result = self.state.get("pendingResult")
        if not result:
            path = Path(self.state["resultPath"])
            if path.is_symlink() or not path.is_file():
                raise WorkflowError("Agent did not write a decision file")
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or not result["summary"].strip():
                raise WorkflowError("Decision requires a non-empty summary")
            self.state["pendingResult"] = result
            self.save()
        decision = result.get("decision")
        if self.role in {"reviewer", "manager"}:
            if __package__:
                from .preparation_workspace import assert_preparation_verified, preparation_snapshot
            else:
                from preparation_workspace import assert_preparation_verified, preparation_snapshot
            snapshot = assert_preparation_verified(self) if decision in {"approve", "accept"} else preparation_snapshot(self)
            if snapshot is not None:
                self.state["verificationPreparationEvidence"] = snapshot
                self.save()

        if decision == "blocked" and self.state.get("verificationRecoveryRequired"):
            attempt, limit = self.reserve_recovery_budget("verification_context", "Claim expired during host verification")
            directory = self.root / ".ralph/verification" / self.state.get("worktreeClaimId", self.state["claimId"]) / "verification-retry" / str(attempt)
            atomic_json(directory / "execution.json", self.state)
            atomic_json(directory / "result.json", result)
            self.state["verificationRetry"] = {"attempt": attempt, "limit": limit, "previousResultPath": str(directory / "result.json"),
                "instruction": "Ralph refreshed the expired verification Context. Preserve previous work and verified evidence, rerun the unfinished host/browser verification, and produce a fresh decision."}
            self.state.pop("pendingResult", None)
            self.state.pop("verificationRecoveryRequired", None)
            Path(self.state["resultPath"]).unlink(missing_ok=True)
            self.save()
            raise ReviewRepairScheduled("Verification Context refreshed; rerun unfinished checks with retained work")
        if decision == "blocked":
            self.note(result["summary"])
            self.command("release_claim", {"reason": result["summary"]})
            self.state["releasedClaim"] = True
            self.defer_execution(result["summary"])
            raise RecoveryDeferred("Agent deferred this Task: " + result["summary"][:1000])
        if self.role != "worker" and run("git", "status", "--porcelain", "--untracked-files=no", cwd=Path(self.state["cwd"])):
            raise WorkflowError("Reviewer/Manager modified tracked files; decision is not valid for the pinned commit")
        if self.role in {"reviewer", "manager"}:
            cwd = Path(self.state["cwd"])
            expected = self.state.get("expectedHeadSha" if self.role == "reviewer" else "expectedMergeSha")
            if expected and run("git", "rev-parse", "HEAD", cwd=cwd) != expected:
                raise WorkflowError("Reviewer/Manager changed the pinned product HEAD")
            untracked = run("git", "ls-files", "--others", "--exclude-standard", "-z", cwd=cwd).split("\0")
            if any(name and not name.startswith(".ralph/") and name != ".ralph-result.json" for name in untracked):
                raise WorkflowError("Reviewer/Manager added untracked product files; use the preparation workspace or artifact directory")
        if self.role == "worker" and decision == "complete":
            try:
                self.finish_worker(result)
            except WorkerRepairNeeded as error:
                self.schedule_worker_repair(error)
                raise WorkerRepairScheduled(str(error)) from error
        elif self.role == "reviewer" and decision in {"approve", "reject"}:
            try:
                self.finish_review(result)
            except RemoteError as error:
                if error.status not in {405, 409} or not error.operation.endswith("/merge") or result["decision"] != "approve":
                    raise
                pr = self.github(f"{self.repo_path()}/pulls/{self.state['pullRequests'][-1]['number']}")
                if pr.get("merged"):
                    raise RetryLater("PR was merged concurrently; rechecking the saved review") from error
                if pr["head"]["sha"] != self.state["expectedHeadSha"]:
                    raise WorkflowError("PR head changed after review; obtain a fresh review") from error
                if pr.get("mergeable") is not False:
                    raise RetryLater("Merge was refused; conflict is not confirmed. Retain review and retry") from error
                self.schedule_review_repair(error)
                raise ReviewRepairScheduled("Confirmed PR conflict; return the saved review to Reviewer") from error
        elif self.role == "manager" and decision in {"accept", "reject"}:
            self.note(result["summary"])
            if decision == "accept":
                self.transition("accept_task", {"verification": {"mergeSha": self.state["expectedMergeSha"], "evidence": result["summary"]}})
            else:
                self.transition("reject_task", {"reason": result["summary"]})
        else:
            raise WorkflowError(f"Invalid {self.role} decision")
        self.complete_execution()

    def schedule_review_repair(self, error: WorkflowError) -> None:
        self.reserve_recovery_budget("review-repair", str(error))
        attempt = self.state.get("reviewRepairAttempts", 0) + 1
        limit = self.config.get("retry", {}).get("maxNoProgressAttempts", 3)
        if attempt > limit:
            self.defer_execution(f"Reviewer conflict reassessment limit reached ({limit})")
            raise RecoveryDeferred("Reviewer reassessment limit reached")
        directory = self.root / ".ralph/verification" / self.state["claimId"] / "review-repair" / str(attempt)
        atomic_json(directory / "execution.json", self.state)
        atomic_json(directory / "result.json", self.state["pendingResult"])
        self.state["reviewRepair"] = {"code": "merge_conflict", "reason": str(error),
            "attempt": attempt, "limit": limit, "previousResultPath": str(directory / "result.json"),
            "previousExecutionPath": str(directory / "execution.json"),
            "headSha": self.state["expectedHeadSha"]}
        self.state["reviewRepairAttempts"] = attempt
        self.state.pop("pendingResult")
        self.state["executionError"] = {"phase": "merge", "reason": "PR has a confirmed merge conflict"}
        self.save()

    def schedule_worker_repair(self, error: WorkerRepairNeeded) -> None:
        if self.role != "worker" or self.state.get("commitSha") or self.state.get("prNumber") or self.state.get("finalCommand"):
            raise WorkflowError("Automatic Worker repair is only allowed before commit and PR operations")
        self.reserve_recovery_budget("worker-repair", str(error))
        limit = self.config.get("retry", {}).get("maxNoProgressAttempts", 3)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise WorkflowError("retry.maxNoProgressAttempts must be a positive integer")
        attempt = self.state.get("workerRepairAttempts", 0) + 1
        if attempt > limit:
            self.defer_execution(f"Worker repair limit reached ({limit}): {error}")
            raise RecoveryDeferred("Worker repair limit reached")
        directory = self.root / ".ralph/verification" / self.state.get("worktreeClaimId", self.state["claimId"]) / "worker-repair" / str(attempt)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(directory / "execution.json", self.state)
        atomic_json(directory / "result.json", self.state["pendingResult"])
        repair = {"attempt": attempt, "limit": limit, "code": error.code, "reason": str(error),
            "diagnostics": error.diagnostics, "previousResultPath": str(directory / "result.json"),
            "previousExecutionPath": str(directory / "execution.json"), "occurredAt": int(time.time() * 1000)}
        self.state["workerRepair"] = repair
        self.state["workerRepairAttempts"] = attempt
        self.state["executionError"] = {"phase": "precommit", "reason": str(error)}
        self.state.pop("pendingResult")
        self.save()

    def check_worker_diff(self, cwd: Path) -> None:
        result = subprocess.run(["git", "diff", "--cached", "--check", "--no-ext-diff", "--no-textconv"],
            cwd=cwd, env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, check=False)
        if not result.returncode:
            return
        diagnostics = []
        issues = {"trailing whitespace.", "space before tab in indent.", "new blank line at EOF.", "leftover conflict marker"}
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"(.+):(\d+): (.+)", line)
            if match and match[3].rstrip('.') in {issue.rstrip('.') for issue in issues}:
                diagnostics.append({"path": match[1], "line": int(match[2]), "issue": match[3]})
        if result.returncode == 2 and diagnostics and not result.stderr.strip():
            # Only location/issue headers are returned; source lines may contain credentials.
            raise WorkerRepairNeeded("precommit_diff", "Pre-commit diff check failed", diagnostics)
        raise WorkflowError(f"git diff --cached --check failed (exit {result.returncode})")

    def unchanged_merge_config(self, cwd: Path, file: str) -> bool:
        """Allow only tracked preview/MCP config inherited verbatim from the base branch."""
        if file not in {".ralph/config.json", ".mcp.json"} or (cwd / file).is_symlink():
            return False
        merge = subprocess.run(["git", "rev-parse", "--verify", "MERGE_HEAD"],
            cwd=cwd, capture_output=True, text=True, check=False)
        sha = merge.stdout.strip()
        if merge.returncode or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return False
        base = self.workflow["baseBranch"]
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", sha, f"origin/{base}"],
            cwd=cwd, capture_output=True, check=False)
        if ancestor.returncode:
            return False
        source = run("git", "ls-tree", "-z", sha, "--", file, cwd=cwd)
        if not source.startswith("100644 blob ") or "\t" not in source:
            return False
        blob = source.split("\t", 1)[0].split()[-1]
        expected = f"100644 {blob} 0\t{file}\0"
        if run("git", "ls-files", "--stage", "-z", "--", file, cwd=cwd) != expected:
            return False
        unchanged = subprocess.run(["git", "diff", "--quiet", "--no-ext-diff", "--no-textconv", "--", file],
            cwd=cwd, capture_output=True, check=False)
        return unchanged.returncode == 0

    def adopt_worker_base(self, cwd: Path, head: str) -> bool:
        """Recognize only a forward move through commits already on the freshly fetched base."""
        initial = self.state["initialHeadSha"]
        if self.role != "worker" or any(self.state.get(key) for key in ("commitSha", "prNumber", "finalCommand")):
            return False
        def ancestor(older, newer):
            result = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                cwd=cwd, capture_output=True, check=False)
            if result.returncode not in (0, 1):
                raise WorkflowError("Cannot verify worker base ancestry; execution retained")
            return result.returncode == 0
        if not ancestor(initial, head):
            return False
        branch = self.workflow["baseBranch"]
        self.git_remote("fetch", f"refs/heads/{branch}:refs/remotes/origin/{branch}", self.root)
        if not ancestor(head, f"origin/{branch}"):
            return False
        remote_head = run("git", "rev-parse", f"origin/{branch}", cwd=cwd)
        self.state.setdefault("upstreamBaseAdvances", []).append({"from": initial, "to": head, "verifiedBaseSha": remote_head})
        self.state["initialHeadSha"] = head
        self.save()
        print(f"Workerの上流commit取り込みを確認しました。base={branch} from={initial} to={head}", file=sys.stderr, flush=True)
        return True

    def finish_worker(self, result: dict) -> None:
        cwd = Path(self.state["cwd"])
        if self.state["task"].get("completionMode") == "evidence":
            changed = run("git", "status", "--porcelain", cwd=cwd)
            head = run("git", "rev-parse", "HEAD", cwd=cwd)
            if result.get("files") != [] or changed or head != self.state["initialHeadSha"]:
                raise WorkerRepairNeeded("evidence_manifest", "Evidence Tasks require files: [], unchanged source and no commits; report operational evidence instead of a fake PR")
            self.note(result["summary"])
            self.transition("complete_task")
            return
        if not self.state.get("commitSha"):
            head = run("git", "rev-parse", "HEAD", cwd=cwd)
            if head != self.state["initialHeadSha"]:
                message = run("git", "log", "-1", "--format=%B", cwd=cwd)
                claim_ids = [self.state["claimId"], *self.state.get("previousClaimIds", [])]
                if any(f"Wacha-Claim: {claim}" in message.splitlines() for claim in claim_ids):
                    self.state["commitSha"] = head
                    self.save()
                elif not self.adopt_worker_base(cwd, head):
                    raise WorkflowError("Unexpected commit in worker worktree: HEAD is neither a saved Claim commit nor a forward move within the remote base")
        if not self.state.get("commitSha"):
            files = result.get("files")
            message = result.get("commitMessage")
            if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files) or not isinstance(message, str) or not message.strip():
                raise WorkerRepairNeeded("result_manifest", "Worker decision requires files and commitMessage")
            for file in files:
                path = Path(file)
                if not file or path.is_absolute() or ".." in path.parts or path.as_posix() != file or not path.parts:
                    raise WorkflowError(f"Invalid commit manifest path: {file!r}")
                if not (cwd / path).resolve().is_relative_to(cwd.resolve()):
                    raise WorkflowError(f"Commit path escapes worktree: {file!r}")
                if path.parts[0] == ".git" or file.startswith((".ralph", ".mcp.json", ".claude")):
                    if not self.unchanged_merge_config(cwd, file):
                        raise WorkflowError(f"Protected commit manifest path: {file!r}; only unchanged base-branch config in a pending merge is permitted")
            # git mv / git rm may already have removed the source from the index.
            # Keep those deletions in the manifest, but do not add them a second time.
            indexed = set(run("git", "ls-files", "-z", cwd=cwd).split("\0"))
            deleted = set(run("git", "diff", "--cached", "--no-renames", "--diff-filter=D",
                "--name-only", "-z", cwd=cwd).split("\0"))
            to_add = []
            for file in files:
                # Inherited protected config is already staged and verified against MERGE_HEAD.
                if file in {".ralph/config.json", ".mcp.json"}:
                    continue
                path = cwd / file
                if path.exists() or path.is_symlink() or file in indexed:
                    to_add.append(file)
                elif file not in deleted:
                    raise WorkerRepairNeeded("missing_manifest_path", "Commit manifest path is missing and is not a staged deletion")
            if to_add:
                run("git", "add", "--", *to_add, cwd=cwd)
            # Disabling rename detection checks both source and destination paths.
            staged = set(run("git", "diff", "--cached", "--no-renames", "--name-only", "-z", cwd=cwd).split("\0")) - {""}
            if not staged or not staged.issubset(set(files)):
                raise WorkerRepairNeeded("staged_manifest", "Staged paths must match the explicit file manifest")
            if run("git", "diff", "--name-only", cwd=cwd):
                raise WorkerRepairNeeded("tracked_manifest", "Worker left tracked changes outside the commit manifest")
            untracked = run("git", "ls-files", "--others", "--exclude-standard", cwd=cwd).splitlines()
            if any(file not in files and not file.startswith((".ralph/", ".ralph-result.json")) for file in untracked):
                raise WorkerRepairNeeded("untracked_manifest", "Worker left new files outside the commit manifest")
            self.check_worker_diff(cwd)
            self.phase = "git:commit"
            run("git", "commit", "-m", message, "-m", f"Wacha-Claim: {self.state['claimId']}", cwd=cwd)
            self.state["commitSha"] = run("git", "rev-parse", "HEAD", cwd=cwd)
            self.save()
        self.renew()
        self.git_remote("push", f"HEAD:refs/heads/{self.state['branch']}", cwd)
        if not self.state.get("prNumber"):
            owner = self.workflow["repository"].split("/")[0]
            query = urllib.parse.urlencode({"head": f"{owner}:{self.state['branch']}", "base": self.workflow["baseBranch"], "state": "all"})
            prs = self.github(f"{self.repo_path()}/pulls?{query}")
            if prs:
                pr = prs[0]
            else:
                if self.state.get("prCreation", {}).get("outcome") == "unknown":
                    raise WorkflowError("PR creation outcome is unknown; lookup found no PR, so a second POST is refused")
                self.state["prCreation"] = {"outcome": "unknown", "headSha": self.state["commitSha"]}
                self.save()
                try:
                    pr = self.github(f"{self.repo_path()}/pulls", "POST", {
                        "title": self.state["task"]["title"], "body": f"## 実装報告\n\n{result['summary']}\n\n---\n\n- Wacha Task: `{self.state['task']['id']}`",
                        "head": self.state["branch"], "base": self.workflow["baseBranch"]})
                except RemoteError:
                    self.state["prCreation"]["outcome"] = "failed"
                    self.save()
                    raise
            self.state["prCreation"] = {"outcome": "success", "number": pr["number"]}
            self.state["prNumber"] = pr["number"]
            self.save()
        self.command("register_task_pull_request", {"number": self.state["prNumber"]})
        self.note(f"{result['summary']}\n\nCommit: {self.state['commitSha']}\nPR: https://github.com/{self.workflow['repository']}/pull/{self.state['prNumber']}")
        self.transition("complete_task")

    def finish_review(self, result: dict) -> None:
        if self.state["task"].get("completionMode") == "evidence":
            self.note(result["summary"])
            if result["decision"] == "approve":
                self.transition("reviewed_task")
            else:
                self.transition("reject_task", {"reason": result["summary"]})
            return
        number = self.state["pullRequests"][-1]["number"]
        path = f"{self.repo_path()}/pulls/{number}"
        pr = self.github(path)
        sha = self.state["expectedHeadSha"]
        if pr["head"]["sha"] != sha:
            raise WorkflowError("PR head changed after review; a new review is required")
        if result["decision"] == "reject":
            if pr.get("merged"):
                raise WorkflowError("Cannot request changes after PR merge")
            self.post_review_comment(path, sha, "reject", result["summary"])
            self.note(result["summary"])
            self.transition("reject_task", {"reason": result["summary"]})
            return
        if not pr.get("merged"):
            if pr["state"] != "open" or pr.get("draft"):
                raise WorkflowError("PR is closed or draft")
            required = self.workflow["requiredChecks"]
            if required:
                checks = self.pages(f"{self.repo_path()}/commits/{sha}/check-runs?filter=latest", "check_runs")
                statuses = self.pages(f"{self.repo_path()}/commits/{sha}/statuses")
                states = {}
                for status in statuses:
                    states.setdefault(status["context"], status["state"])
                for check in checks:
                    state = check["conclusion"] if check["status"] == "completed" else "pending"
                    # Ambiguous duplicate names fail closed.
                    states[check["name"]] = state if check["name"] not in states or states[check["name"]] == "success" else states[check["name"]]
                if any(states.get(name) != "success" for name in required):
                    raise RetryLater("Required CI has not passed; retain decision and retry without launching an Agent")
            self.state["reviewCommentId"] = self.post_review_comment(path, sha, "approve", result["summary"])
            self.save()
            self.renew()
            merged = self.github(f"{path}/merge", "PUT", {"sha": sha, "merge_method": "squash"})
            if not merged.get("merged"):
                current = self.github(path)
                if current.get("mergeable") is False and current["head"]["sha"] == sha:
                    self.schedule_review_repair(WorkflowError("GitHub confirmed that this PR cannot merge due to conflicts"))
                    raise ReviewRepairScheduled("Confirmed PR conflict; return to Reviewer")
                raise RetryLater("GitHub has not merged the PR; retain review and retry")
        if not self.state.get("reviewCommentId"):
            self.state["reviewCommentId"] = self.post_review_comment(path, sha, "approve", result["summary"])
            self.save()
        self.note(result["summary"])
        self.transition("reviewed_task", {"expectedHeadSha": sha, "reviewCommentId": self.state["reviewCommentId"]})

    def post_review_comment(self, path: str, sha: str, decision: str, body: str) -> int:
        marker = "<!-- task-review: " + json.dumps({"taskId": self.state["task"]["id"],
            "claimId": self.state["claimId"], "headSha": sha, "decision": decision}, separators=(",", ":")) + " -->"
        comments_path = path.replace("/pulls/", "/issues/") + "/comments"
        reviews = self.pages(comments_path)
        previous = next((review for review in reversed(reviews) if marker in review.get("body", "")), None)
        if previous:
            self.state["reviewPosting"] = {"outcome": "success", "marker": marker, "id": previous["id"]}
            self.save()
            return previous["id"]
        text = (f"## レビュー結果: **{decision}**\n\n{body}\n\n---\n\n"
            f"- Reviewer: {self.principal}\n- Task: `{self.state['task']['id']}`\n"
            f"- Head: `{sha}`\n\n{marker}")
        posting = self.state.get("reviewPosting", {})
        if posting.get("outcome") == "unknown":
            raise WorkflowError("Review comment outcome is unknown; lookup found no matching receipt, so a second POST is refused")
        if posting.get("outcome") == "success" and posting.get("marker") == marker:
            return posting["id"]
        self.state["reviewPosting"] = {"outcome": "unknown", "marker": marker}
        self.save()
        try:
            review = self.github(comments_path, "POST", {"body": text})
        except RemoteError:
            self.state["reviewPosting"]["outcome"] = "failed"
            self.save()
            raise
        self.state["reviewPosting"] = {"outcome": "success", "marker": marker, "id": review["id"]}
        self.save()
        return review["id"]

    def retain_execution(self, reason: str) -> None:
        """Keep released/abandoned work while freeing the fixed identity checkout path."""
        archive = self.state_path.parent / "retained" / f"{self.role}-{self.state['claimId']}.json"
        self.state["retainedReason"] = reason
        self.save()
        atomic_json(archive, self.state)
        try:
            self.state["cleanup"] = self.cleanup_worktree(self.state)
            self.state.pop("cleanupError", None)
            atomic_json(archive, self.state)
            self.clear()
            return
        except (WorkflowError, OSError) as error:
            self.state["cleanup"] = "retained"
            self.state["cleanupError"] = str(error)
            atomic_json(archive, self.state)
        directory = Path(self.state.get("cwd", ""))
        if self.state.get("cwd") and not self.state.get("legacyWorktree"):
            expected = self.expected_worktree(self.state)
            destination = expected.parent / "retained" / f"{self.role}-{self.state['claimId']}"
            if directory != expected and directory != destination:
                raise WorkflowError("Cannot retain an unexpected Worktree path")
            if directory.is_symlink() or destination.is_symlink():
                raise WorkflowError("Cannot retain redirected Worktree paths")
            self.state["retainedCwd"] = str(destination)
            self.save()
            atomic_json(archive, self.state)
            if directory == expected and directory.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                run("git", "worktree", "move", str(directory), str(destination), cwd=self.root)
            if destination.exists():
                registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
                if f"worktree {destination}" not in registered:
                    raise WorkflowError("Retained path is not a registered Worktree")
                self.state["cwd"] = str(destination)
                if self.state.get("resultPath"):
                    self.state["resultPath"] = str(destination / ".ralph-result.json")
            atomic_json(archive, self.state)
        self.clear()

    def abandon(self) -> None:
        if not self.state:
            return
        try:
            self.command("release_claim", {"reason": "Operator abandoned the pending execution for recovery"})
        except RetryLater:
            raise
        except WorkflowError as error:
            if not any(code in str(error) for code in ["CLAIM_EXPIRED", "CLAIM_NOT_FOUND", "CLAIM_NOT_OWNED"]):
                raise
        self.state["releasedClaim"] = True
        self.retain_execution("Operator abandoned the pending execution for recovery")

    def save_error(self, phase: str, reason: str, retryable: bool = False) -> None:
        atomic_json(self.state_path.with_suffix(".error.json"), {
            "phase": phase + "/" + self.phase, "reason": reason, "retryable": retryable,
            "principal": self.principal, "role": self.role,
            "taskId": self.state.get("task", {}).get("id"), "claimId": self.state.get("claimId"),
            "prNumber": self.state.get("prNumber"), "occurredAt": int(time.time() * 1000)})

    def release_on_error(self, reason: str) -> None:
        # A failed result application must remain resumable, including when a
        # subsequent prepare/renew discovers that its Claim has expired.
        if self.state.get("executionError") or self.state.get("claimRecovery") or "CLAIM_EXPIRED" in reason or "execution retained" in reason:
            return
        if self.state and not self.state.get("pendingResult"):
            try:
                self.note(reason)
                self.command("release_claim", {"reason": reason})
                self.clear()
            except WorkflowError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "finish", "renew", "abandon", "watch", "cleanup", "recover", "agent-failed"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--principal")
    parser.add_argument("--role", choices=["worker", "reviewer", "manager"], required=True)
    args = parser.parse_args()
    workflow = None
    try:
        workflow = Workflow(args.config, args.role, args.principal)
        if args.command == "prepare":
            print(json.dumps(workflow.prepare(), ensure_ascii=False))
        elif args.command == "finish":
            workflow.finish()
        elif args.command == "recover":
            workflow.apply_recovery()
        elif args.command == "agent-failed":
            workflow.schedule_operation_recovery("agent", "Agent process failed during bounded recovery; inspect saved execution and result")
            return 76
        elif args.command == "abandon":
            workflow.abandon()
        elif args.command == "cleanup":
            workflow.retry_completed_cleanup()
        elif args.command == "watch":
            while True:
                time.sleep(60)
                workflow = Workflow(args.config, args.role, args.principal)
                if not workflow.state:
                    return 0
                workflow.renew()
                runtime = read_object(workflow.root / ".ralph/runtime.json")
                activity = runtime.get("agents", {}).get(workflow.principal, {}).get(workflow.role, {}).get("activity", {})
                if activity.get("phase") == "agent_running":
                    try:
                        pid = int(activity["pid"])
                        os.kill(pid, 0)
                        title = re.sub(r"\s+", " ", activity.get("title", ""))
                        elapsed = int(time.time()) - activity.get("updatedAt", int(time.time()))
                        print(f"Agentプロセス稼働中 pid={pid} 経過={elapsed}秒 Task={title} [{activity.get('taskId', '')}]", flush=True)
                    except (ProcessLookupError, ValueError, KeyError):
                        pass
        else:
            workflow.renew()
        return 0
    except RecoveryDeferred as error:
        print("Task保留: " + brief_reason(str(error)) + " 記録=" + str(workflow.state_path.parent / "deferred"), file=sys.stderr)
        return 79
    except WorkerRepairScheduled as error:
        repair = workflow.state["workerRepair"]
        print(f"Worker repair scheduled ({repair['attempt']}/{repair['limit']}): {error}", file=sys.stderr)
        for diagnostic in repair["diagnostics"]:
            print(json.dumps(diagnostic, ensure_ascii=False), file=sys.stderr)
        return 76
    except ReviewRepairScheduled as error:
        print(str(error), file=sys.stderr)
        return 76
    except RetryLater as error:
        if workflow is not None:
            workflow.save_error(args.command, str(error), retryable=True)
        print(str(error), file=sys.stderr)
        return 75
    except (WorkflowError, ValueError, KeyError, OSError) as error:
        if isinstance(error, WorkflowError):
            reason = str(error)
        elif isinstance(error, json.JSONDecodeError):
            reason = f"JSON parse failed at line {error.lineno}, column {error.colno}; check config/execution/result JSON"
        elif isinstance(error, KeyError):
            field = str(error.args[0])
            reason = "Required field missing: " + (field if re.fullmatch(r"[A-Za-z0-9_.-]+", field) else "unknown field")
        elif isinstance(error, OSError):
            reason = f"{type(error).__name__}: {os.strerror(error.errno) if error.errno else 'local file/OS operation failed'}"
        else:
            reason = f"{type(error).__name__}: local configuration/state validation failed"
        if workflow is not None:
            if args.command == "finish" and workflow.state and workflow.state_path.exists():
                workflow.state["executionError"] = {"phase": "finish", "reason": reason, "occurredAt": int(time.time() * 1000)}
                workflow.save()
            workflow.save_error(args.command, reason)
            if args.command in {"prepare", "finish", "recover", "agent-failed"} and workflow.state.get("cwd"):
                try:
                    workflow.schedule_operation_recovery(args.command, reason)
                    print("Agent recovery scheduled: " + reason, file=sys.stderr)
                    return 76
                except RecoveryDeferred as deferred:
                    print("Task保留: " + brief_reason(str(deferred)) + " 記録=" + str(workflow.state_path.parent / "deferred"), file=sys.stderr)
                    return 79
                except (WorkflowError, OSError, KeyError, ValueError):
                    pass  # Invalid ownership/configuration cannot authorize a recovery execution.
        print(f"{args.role} {args.command}: {reason}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
