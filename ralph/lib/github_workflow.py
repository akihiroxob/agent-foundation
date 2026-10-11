#!/usr/bin/env python3
"""Trusted runner for Git/PR mutations; agents only produce a decision file."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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


def run(*args: str, cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        # Git stderr can contain credential-bearing remote URLs; don't echo it.
        raise WorkflowError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout.strip()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Workflow:
    def __init__(self, config_path: Path, role: str):
        self.config_path = config_path.resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.role = role
        root = self.config.get("projectRoot")
        self.root = (self.config_path.parent.parent / root).resolve() if root else self.config_path.parent.parent
        self.state_path = self.root / ".ralph" / "github-state" / f"{role}.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        self.principal = self.config["roles"][role]["agentName"]
        names = self.token_names()
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*_TOKEN", name) for name in names.values()):
            raise WorkflowError("github.tokenEnvs values must be dedicated *_TOKEN environment variable names")
        self.workflow: dict = {}

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def host_commands(self) -> list[list[str]]:
        if __package__:
            from .host_verification import commands
        else:
            from host_verification import commands
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
                "--role", self.role, "--claim-id", claim_id], "timeout": 3660000}
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
            raise WorkflowError(f"Remote service returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RetryLater("Remote service unavailable") from error

    def mcp(self, name: str, arguments: dict) -> dict:
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
        return self.request(f"https://api.github.com{path}", self.github_token(), method, body)

    def git_remote(self, operation: str, refspec: str, cwd: Path) -> str:
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
        self.host_commands()  # Reject invalid host configuration before acquiring a Claim.
        self.retry_completed_cleanup()
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
            if self.role == "manager":
                tasks = [task for task in tasks if task["status"] == "wait_accept"]
            if self.role == "worker":
                tasks.sort(key=lambda task: task["status"] != "rejected")
            if not tasks:
                return {"idle": True}
            task = tasks[0]
            command = {"worker": "claim_task", "reviewer": "claim_review", "manager": "claim_acceptance"}[self.role]
            claim = self.mcp(command, {"taskId": task["id"], "requestId": f"ralph:claim:{self.role}:{os.urandom(16).hex()}"})
            self.state = {"task": task, "claimId": claim["claimId"], "projectId": project["id"]}
            self.save()
        history = self.mcp("list_task_pull_requests", {"taskId": self.state["task"]["id"]})["pullRequests"]
        self.state["pullRequests"] = history
        branch = self.workflow["baseBranch"]
        self.git_remote("fetch", f"refs/heads/{branch}:refs/remotes/origin/{branch}", self.root)
        if self.role == "worker":
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
        directory = self.root / ".ralph" / "worktrees" / f"{self.role}-{self.state['task']['id']}-{worktree_claim[:8]}"
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
        if not self.state.get("setupDone"):
            commands = self.config.get("github", {}).get("setupCommands", [])
            if not isinstance(commands, list) or any(not isinstance(command, list) or not command or
                    any(not isinstance(arg, str) for arg in command) for command in commands):
                raise WorkflowError("github.setupCommands must be an array of argument arrays")
            for command in commands:
                run(*command, cwd=directory)
            self.state["setupDone"] = True
        self.state["cwd"] = str(directory)
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
            "browserVerificationSkill": str(skill) if browser_enabled else None}

    def renew(self) -> None:
        if self.state:
            # renew_claim has no taskId argument.
            self.mcp("renew_claim", {"claimId": self.state["claimId"]})

    def renew_or_reclaim(self) -> None:
        if not self.state:
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

    def cleanup_worktree(self, execution: dict) -> str:
        """Remove only this runner's clean, completed checkout; never force removal."""
        operations = {"worker": {"complete_task"}, "reviewer": {"reviewed_task", "reject_task"},
                      "manager": {"accept_task", "reject_task"}}
        if not execution.get("completed") or execution.get("finalCommand", {}).get("name") not in operations[self.role]:
            raise WorkflowError("Execution is not successfully completed")
        directory = Path(execution["cwd"])
        worktree_claim = execution.get("worktreeClaimId", execution["claimId"])
        expected = self.root / ".ralph" / "worktrees" / f"{self.role}-{execution['task']['id']}-{worktree_claim[:8]}"
        if directory != expected or directory.resolve() != expected.absolute():
            raise WorkflowError("Worktree path is not the expected Ralph checkout")
        if not directory.exists():
            return "removed"
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if f"worktree {directory}" not in registered:
            raise WorkflowError("Checkout is not a registered Ralph worktree")
        sha = execution.get({"worker": "commitSha", "reviewer": "expectedHeadSha", "manager": "expectedMergeSha"}[self.role])
        if not sha or run("git", "rev-parse", "HEAD", cwd=directory) != sha:
            raise WorkflowError("Worktree contains an unexpected commit")
        if run("git", "status", "--porcelain", "--untracked-files=no", cwd=directory):
            raise WorkflowError("Worktree contains uncommitted changes")
        runtime_paths = {".ralph/mcp.json", ".ralph-result.json"}
        untracked = set(run("git", "ls-files", "--others", "--exclude-standard", "-z", cwd=directory).split("\0")) - {""}
        if untracked - runtime_paths:
            raise WorkflowError("Worktree contains untracked files")
        tracked = set(run("git", "ls-files", "-z", cwd=directory).split("\0"))
        if tracked & runtime_paths or (directory / ".ralph").is_symlink():
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
        for relative in runtime_paths:
            (directory / relative).unlink(missing_ok=True)
        run("git", "worktree", "remove", str(directory), cwd=self.root)
        return "removed"

    def cleanup_record(self, path: Path, execution: dict) -> None:
        try:
            execution["cleanup"] = self.cleanup_worktree(execution)
            execution.pop("cleanupError", None)
        except (WorkflowError, OSError) as error:
            execution["cleanup"] = "retained"
            execution["cleanupError"] = str(error)
            print(f"{self.role}: completed worktree retained; {error}", file=sys.stderr)
        atomic_json(path, execution)

    def retry_completed_cleanup(self) -> None:
        for path in (self.state_path.parent / "completed").glob(f"{self.role}-*.json"):
            execution = json.loads(path.read_text(encoding="utf-8"))
            if execution.get("completed") and execution.get("cleanup") != "removed":
                self.cleanup_record(path, execution)

    def complete_execution(self) -> None:
        # Persist success before cleanup so a crash never renews a completed Claim
        # or reruns the Agent. The final result remains available after removal.
        self.state["completed"] = True
        self.save()
        archive = self.state_path.parent / "completed" / f"{self.role}-{self.state['claimId']}.json"
        atomic_json(archive, self.state)
        self.cleanup_record(archive, self.state)
        self.clear()

    def finish(self) -> None:
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
        if decision == "blocked":
            self.note(result["summary"])
            self.command("release_claim", {"reason": result["summary"]})
            self.clear()
            raise WorkflowError("Agent reported an environment blocker; role paused")
        if self.role != "worker" and run("git", "status", "--porcelain", "--untracked-files=no", cwd=Path(self.state["cwd"])):
            raise WorkflowError("Reviewer/Manager modified tracked files; decision is not valid for the pinned commit")
        if self.role == "worker" and decision == "complete":
            self.finish_worker(result)
        elif self.role == "reviewer" and decision in {"approve", "reject"}:
            self.finish_review(result)
        elif self.role == "manager" and decision in {"accept", "reject"}:
            self.note(result["summary"])
            if decision == "accept":
                self.transition("accept_task", {"verification": {"mergeSha": self.state["expectedMergeSha"], "evidence": result["summary"]}})
            else:
                self.transition("reject_task", {"reason": result["summary"]})
        else:
            raise WorkflowError(f"Invalid {self.role} decision")
        self.complete_execution()

    def finish_worker(self, result: dict) -> None:
        cwd = Path(self.state["cwd"])
        if not self.state.get("commitSha"):
            head = run("git", "rev-parse", "HEAD", cwd=cwd)
            if head != self.state["initialHeadSha"]:
                message = run("git", "log", "-1", "--format=%B", cwd=cwd)
                claim_ids = [self.state["claimId"], *self.state.get("previousClaimIds", [])]
                if not any(f"Wacha-Claim: {claim}" in message.splitlines() for claim in claim_ids):
                    raise WorkflowError("Unexpected commit in worker worktree")
                self.state["commitSha"] = head
                self.save()
        if not self.state.get("commitSha"):
            files = result.get("files")
            message = result.get("commitMessage")
            if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files) or not isinstance(message, str) or not message.strip():
                raise WorkflowError("Worker decision requires files and commitMessage")
            for file in files:
                path = Path(file)
                if path.is_absolute() or ".." in path.parts or file.startswith((".git", ".ralph", ".mcp.json", ".claude")):
                    raise WorkflowError("Invalid commit manifest path")
                if not (cwd / path).resolve().is_relative_to(cwd.resolve()):
                    raise WorkflowError("Commit path escapes worktree")
            # git mv / git rm may already have removed the source from the index.
            # Keep those deletions in the manifest, but do not add them a second time.
            indexed = set(run("git", "ls-files", "-z", cwd=cwd).split("\0"))
            deleted = set(run("git", "diff", "--cached", "--no-renames", "--diff-filter=D",
                "--name-only", "-z", cwd=cwd).split("\0"))
            to_add = []
            for file in files:
                path = cwd / file
                if path.exists() or path.is_symlink() or file in indexed:
                    to_add.append(file)
                elif file not in deleted:
                    raise WorkflowError("Commit manifest path is missing and is not a staged deletion")
            if to_add:
                run("git", "add", "--", *to_add, cwd=cwd)
            # Disabling rename detection checks both source and destination paths.
            staged = set(run("git", "diff", "--cached", "--no-renames", "--name-only", "-z", cwd=cwd).split("\0")) - {""}
            if not staged or not staged.issubset(set(files)):
                raise WorkflowError("Staged paths must match the explicit file manifest")
            if run("git", "diff", "--name-only", cwd=cwd):
                raise WorkflowError("Worker left tracked changes outside the commit manifest")
            untracked = run("git", "ls-files", "--others", "--exclude-standard", cwd=cwd).splitlines()
            if any(file not in files and not file.startswith((".ralph/", ".ralph-result.json")) for file in untracked):
                raise WorkflowError("Worker left new files outside the commit manifest")
            run("git", "diff", "--cached", "--check", cwd=cwd)
            run("git", "commit", "-m", message, "-m", f"Wacha-Claim: {self.state['claimId']}", cwd=cwd)
            self.state["commitSha"] = run("git", "rev-parse", "HEAD", cwd=cwd)
            self.save()
        self.renew()
        self.git_remote("push", f"HEAD:refs/heads/{self.state['branch']}", cwd)
        if not self.state.get("prNumber"):
            owner = self.workflow["repository"].split("/")[0]
            query = urllib.parse.urlencode({"head": f"{owner}:{self.state['branch']}", "base": self.workflow["baseBranch"], "state": "open"})
            prs = self.github(f"{self.repo_path()}/pulls?{query}")
            pr = prs[0] if prs else self.github(f"{self.repo_path()}/pulls", "POST", {
                "title": self.state["task"]["title"], "body": f"## 実装報告\n\n{result['summary']}\n\n---\n\n- Wacha Task: `{self.state['task']['id']}`",
                "head": self.state["branch"], "base": self.workflow["baseBranch"]})
            self.state["prNumber"] = pr["number"]
            self.save()
        self.command("register_task_pull_request", {"number": self.state["prNumber"]})
        self.note(f"{result['summary']}\n\nCommit: {self.state['commitSha']}\nPR: https://github.com/{self.workflow['repository']}/pull/{self.state['prNumber']}")
        self.transition("complete_task")

    def finish_review(self, result: dict) -> None:
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
                raise RetryLater("GitHub has not merged the PR")
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
            return previous["id"]
        text = (f"## レビュー結果: **{decision}**\n\n{body}\n\n---\n\n"
            f"- Reviewer: {self.principal}\n- Task: `{self.state['task']['id']}`\n"
            f"- Head: `{sha}`\n\n{marker}")
        review = self.github(comments_path, "POST", {"body": text})
        return review["id"]

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
        archive = self.state_path.with_name(f"{self.role}-{time.time_ns()}.json")
        self.state_path.rename(archive)

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
    parser.add_argument("command", choices=["prepare", "finish", "renew", "abandon", "watch"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--role", choices=["worker", "reviewer", "manager"], required=True)
    args = parser.parse_args()
    workflow = Workflow(args.config, args.role)
    try:
        if args.command == "prepare":
            print(json.dumps(workflow.prepare(), ensure_ascii=False))
        elif args.command == "finish":
            workflow.finish()
        elif args.command == "abandon":
            workflow.abandon()
        elif args.command == "watch":
            while True:
                time.sleep(60)
                workflow = Workflow(args.config, args.role)
                if not workflow.state:
                    return 0
                workflow.renew()
        else:
            workflow.renew()
        return 0
    except RetryLater as error:
        print(str(error), file=sys.stderr)
        return 75
    except (WorkflowError, ValueError, KeyError, OSError) as error:
        if args.command == "finish" and workflow.state and workflow.state_path.exists():
            workflow.state["executionError"] = {"phase": "finish", "reason": str(error), "occurredAt": int(time.time() * 1000)}
            workflow.save()
        else:
            workflow.release_on_error(str(error))
        print(str(error), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
