#!/usr/bin/env python3
"""Trusted runner for Git/PR mutations; agents only produce a decision file."""
from __future__ import annotations

import argparse
import json
import os
import re
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


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
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

    def github(self, path: str, method: str = "GET", body: dict | None = None) -> dict | list:
        name = self.token_names().get(self.role)
        token = os.environ.get(name or "")
        if not token:
            raise WorkflowError(f"GitHub credential environment variable {name} is missing")
        return self.request(f"https://api.github.com{path}", token, method, body)

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
                    self.renew()
                return {**self.state, "pending": True, "workflow": self.workflow}
            self.renew()
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
        run("git", "fetch", "origin", branch, cwd=self.root)
        if self.role == "worker":
            if history and history[-1]["state"] == "open":
                pr = self.github(f"{self.repo_path()}/pulls/{history[-1]['number']}")
                if pr["state"] == "open":
                    if pr["head"]["repo"]["full_name"].lower() != self.workflow["repository"].lower():
                        raise WorkflowError("Ralph cannot push to a fork PR")
                    self.state["branch"] = pr["head"]["ref"]
                    run("git", "fetch", "origin", f"refs/pull/{pr['number']}/head", cwd=self.root)
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
                run("git", "fetch", "origin", f"refs/pull/{pr['number']}/head", cwd=self.root)
                start = remote_pr["head"]["sha"]
                self.state["expectedHeadSha"] = start
            else:
                start = pr["mergeSha"]
                if not start or not pr["reviewedHeadSha"]:
                    raise WorkflowError("Acceptance requires a reviewed merged PR")
                run("git", "merge-base", "--is-ancestor", start, f"origin/{branch}", cwd=self.root)
                self.state["expectedMergeSha"] = start
        directory = self.root / ".ralph" / "worktrees" / f"{self.role}-{self.state['task']['id']}-{self.state['claimId'][:8]}"
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
        # Claude discovers MCP servers from its cwd. This contains only an env reference.
        atomic_json(directory / ".ralph" / "mcp.json", {"mcpServers": {"wacha": {"type": "http",
            "url": self.config["wacha"]["url"], "headers": {"Authorization": "Bearer ${WACHA_AGENT_NAME}"}}}})
        result_path = Path(self.state["resultPath"])
        result_path.unlink(missing_ok=True)
        self.save()
        return {**self.state, "workflow": self.workflow}

    def renew(self) -> None:
        if self.state:
            # renew_claim has no taskId argument.
            self.mcp("renew_claim", {"claimId": self.state["claimId"]})

    def note(self, text: str) -> None:
        self.command("add_task_comment", {"body": text})

    def transition(self, name: str, extra: dict | None = None) -> None:
        self.state["finalCommand"] = {"name": name, "extra": extra}
        self.save()
        self.command(name, extra)

    def clear(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def finish(self) -> None:
        if not self.state:
            raise WorkflowError("No active GitHub execution")
        if self.state.get("finalCommand"):
            operation = self.state["finalCommand"]
            self.command(operation["name"], operation["extra"])
            self.clear()
            return
        project = next(item for item in self.mcp("list_projects", {})["projects"] if item["id"] == self.state["projectId"])
        self.workflow = project["workflow"]
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
        self.clear()

    def finish_worker(self, result: dict) -> None:
        cwd = Path(self.state["cwd"])
        if not self.state.get("commitSha"):
            head = run("git", "rev-parse", "HEAD", cwd=cwd)
            if head != self.state["initialHeadSha"]:
                message = run("git", "log", "-1", "--format=%B", cwd=cwd)
                if f"Wacha-Claim: {self.state['claimId']}" not in message:
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
            run("git", "add", "--", *files, cwd=cwd)
            staged = set(run("git", "diff", "--cached", "--name-only", cwd=cwd).splitlines())
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
        run("git", "push", "origin", f"HEAD:refs/heads/{self.state['branch']}", cwd=cwd)
        if not self.state.get("prNumber"):
            owner = self.workflow["repository"].split("/")[0]
            query = urllib.parse.urlencode({"head": f"{owner}:{self.state['branch']}", "base": self.workflow["baseBranch"], "state": "open"})
            prs = self.github(f"{self.repo_path()}/pulls?{query}")
            pr = prs[0] if prs else self.github(f"{self.repo_path()}/pulls", "POST", {
                "title": self.state["task"]["title"], "body": f"Wacha Task: {self.state['task']['id']}\n\n{result['summary']}",
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
        text = f"### Reviewer: {self.principal}\n\nTask: {self.state['task']['id']}\nHead: `{sha}`\nDecision: **{decision}**\n\n{body}\n\n{marker}"
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
        workflow.release_on_error(str(error))
        print(str(error), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
