from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ralph.lib.github_workflow import Workflow, WorkflowError, RetryLater, atomic_json, run
from ralph.lib.limit_reset import retry_seconds

ROOT = Path(__file__).resolve().parents[1]


class FakeWorkflow(Workflow):
    def __init__(self, config, role, remote):
        super().__init__(config, role)
        self.remote = remote
        self.commands = []

    def validate_remote(self):
        pass  # Real local bare repository substitutes for github.com in this test.

    def mcp(self, name, arguments):
        self.commands.append((name, arguments))
        state = self.remote
        if name == "list_projects":
            return {"projects": [{"id": "project", "name": "sample", "workflow": {
                "repository": "org/repo", "baseBranch": "main", "requiredChecks": state.get("requiredChecks", ["verify"])}}]}
        if name == "list_tasks":
            phase = arguments["filter"]["availableFor"]
            expected = {"work": ["todo", "rejected"], "review": ["in_review"], "acceptance": ["wait_accept"]}[phase]
            return {"tasks": [state["task"]] if state["task"]["status"] in expected else []}
        if name.startswith("claim_"):
            state["claims"] += 1
            if name == "claim_task":
                state["task"]["status"] = "doing"
            return {"claimId": f"claim-{state['claims']}"}
        if name == "list_task_pull_requests":
            return {"pullRequests": state["history"]}
        if name == "renew_claim":
            return {}
        key = arguments["requestId"]
        if key in state["receipts"]:
            return state["receipts"][key]
        if name == "register_task_pull_request":
            pr = state["prs"][arguments["number"]]
            old = next((item for item in state["history"] if item["number"] == pr["number"]), None)
            if not old:
                state["history"].append({"number": pr["number"], "state": "open", "repository": "org/repo", "mergeSha": None, "reviewedHeadSha": None})
        elif name == "complete_task":
            state["task"]["status"] = "in_review"
        elif name == "reviewed_task":
            state["task"]["status"] = "wait_accept"
            pr = state["prs"][state["history"][-1]["number"]]
            state["history"][-1].update(state="merged", mergeSha=pr["merge_commit_sha"], reviewedHeadSha=pr["head"]["sha"])
        elif name == "reject_task":
            state["task"]["status"] = "rejected"
        elif name == "accept_task":
            state["task"]["status"] = "accepted"
            state["verification"] = arguments["verification"]
        elif name == "release_claim":
            state["task"]["status"] = "todo"
        state["receipts"][key] = {}
        return {}

    def github(self, path, method="GET", body=None):
        remote = self.remote
        if path == "/user":
            return {"login": "same-account"}
        if "/check-runs?" in path:
            return {"check_runs": [{"name": "verify", "status": "completed", "conclusion": remote["ci"]}]}
        if "/statuses?" in path:
            return []
        if path.endswith("/pulls") and method == "POST":
            number = len(remote["prs"]) + 1
            sha = run("git", "rev-parse", f"refs/heads/{body['head']}", cwd=remote["bare"])
            pr = {"number": number, "head": {"sha": sha, "ref": body["head"], "repo": {"full_name": "org/repo"}},
                  "user": {"login": "same-account"}, "state": "open", "draft": False, "merged": False}
            remote["prs"][number] = pr
            # GitHub exposes the PR head as refs/pull/<number>/head.
            run("git", "update-ref", f"refs/pull/{number}/head", sha, cwd=remote["bare"])
            return pr
        if "/pulls?" in path:
            branch = self.state["branch"]
            return [pr for pr in remote["prs"].values() if pr["head"]["ref"] == branch and not pr["merged"]]
        number = int(path.replace("/issues/", "/pulls/").split("/pulls/")[1].split("/")[0])
        pr = remote["prs"][number]
        if "/comments" in path:
            if method == "POST":
                comment = {"id": len(remote["reviews"]) + 1, "user": {"login": "same-account"}, "body": body["body"]}
                remote["reviews"].append(comment)
                return comment
            return remote["reviews"]
        if path.endswith("/merge"):
            self.assert_equal(body["sha"], pr["head"]["sha"])
            integration = remote["integration"]
            run("git", "fetch", "origin", pr["head"]["ref"], cwd=integration)
            run("git", "merge", "--squash", "FETCH_HEAD", cwd=integration)
            run("git", "commit", "-m", "Integrated PR", cwd=integration)
            run("git", "push", "origin", "main", cwd=integration)
            pr.update(merged=True, state="closed", merge_commit_sha=run("git", "rev-parse", "HEAD", cwd=integration))
            return {"merged": True, "sha": pr["merge_commit_sha"]}
        return pr

    @staticmethod
    def assert_equal(left, right):
        if left != right:
            raise AssertionError(f"{left} != {right}")


class GitHubWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        bare, seed, repo, integration = (self.base / name for name in ("origin.git", "seed", "repo", "integration"))
        seed.mkdir()
        run("git", "init", "-b", "main", cwd=seed)
        run("git", "config", "user.name", "Test", cwd=seed)
        run("git", "config", "user.email", "test@example.test", cwd=seed)
        (seed / "app.txt").write_text("initial\n")
        (seed / ".gitignore").write_text(".ralph/\n.ralph-result.json\n")
        run("git", "add", ".", cwd=seed)
        run("git", "commit", "-m", "Initial", cwd=seed)
        run("git", "clone", "--bare", str(seed), str(bare), cwd=self.base)
        for target in (repo, integration):
            run("git", "clone", str(bare), str(target), cwd=self.base)
            run("git", "config", "user.name", "Test", cwd=target)
            run("git", "config", "user.email", "test@example.test", cwd=target)
        self.repo = repo
        self.config = repo / ".ralph/config.json"
        atomic_json(self.config, {"projectName": "sample", "wacha": {"url": "http://unused/mcp"},
            "roles": {role: {"agentName": role} for role in ("worker", "reviewer", "manager")}, "github": {"enabled": True}})
        self.remote = {"task": {"id": "task-1", "title": "Task", "status": "todo"}, "claims": 0, "history": [],
                       "prs": {}, "reviews": [], "receipts": {}, "bare": bare, "integration": integration, "ci": "success"}

    def tearDown(self):
        self.directory.cleanup()

    def worker(self, text="implemented\n"):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        (Path(context["cwd"]) / "app.txt").write_text(text)
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "AC verified, tests passed", "files": ["app.txt"], "commitMessage": "Implement Task"})
        worker.finish()
        return worker, context

    def test_worktree_pr_review_merge_accept_and_new_pr_after_manager_rejection(self):
        _, context = self.worker()
        self.assertEqual("initial\n", (self.repo / "app.txt").read_text(), "main checkout must remain isolated")
        self.assertEqual("in_review", self.remote["task"]["status"])
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        review = reviewer.prepare()
        atomic_json(Path(review["resultPath"]), {"decision": "approve", "summary": "Diff and tests reviewed"})
        reviewer.finish()
        self.assertEqual("wait_accept", self.remote["task"]["status"])
        manager = FakeWorkflow(self.config, "manager", self.remote)
        acceptance = manager.prepare()
        self.assertEqual(acceptance["expectedMergeSha"], run("git", "rev-parse", "HEAD", cwd=Path(acceptance["cwd"])))
        atomic_json(Path(acceptance["resultPath"]), {"decision": "reject", "summary": "Integrated AC still missing"})
        manager.finish()
        self.worker("corrected\n")
        self.assertEqual([1, 2], [item["number"] for item in self.remote["history"]])
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        review = reviewer.prepare()
        atomic_json(Path(review["resultPath"]), {"decision": "approve", "summary": "Rework verified"})
        reviewer.finish()
        manager = FakeWorkflow(self.config, "manager", self.remote)
        acceptance = manager.prepare()
        atomic_json(Path(acceptance["resultPath"]), {"decision": "accept", "summary": "AC1 passed on integrated commit"})
        manager.finish()
        self.assertEqual("accepted", self.remote["task"]["status"])
        self.assertEqual(acceptance["expectedMergeSha"], self.remote["verification"]["mergeSha"])

    def test_reviewer_rejection_reuses_same_pr_and_records_named_role_comment(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "reject", "summary": "Missing regression test"})
        reviewer.finish()
        self.assertEqual("rejected", self.remote["task"]["status"])
        self.assertIn("Reviewer: reviewer", self.remote["reviews"][0]["body"])
        self.worker("fixed after review\n")
        self.assertEqual(1, len(self.remote["prs"]))
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Regression test verified"})
        reviewer.finish()
        self.assertEqual("wait_accept", self.remote["task"]["status"])
        self.assertEqual(2, len(self.remote["reviews"]))

    def test_no_configured_ci_merges_without_requesting_check_endpoints(self):
        self.remote["requiredChecks"] = []
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed without CI"})
        original = reviewer.github
        def no_checks(path, method="GET", body=None):
            if "/check-runs" in path or "/statuses" in path:
                raise AssertionError("CI endpoints must not be requested without configured checks")
            return original(path, method, body)
        with patch.object(reviewer, "github", side_effect=no_checks):
            reviewer.finish()
        self.assertEqual("wait_accept", self.remote["task"]["status"])
        self.assertTrue(self.remote["prs"][1]["merged"])

    def test_ci_wait_persists_decision_without_new_agent_execution(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed"})
        self.remote["ci"] = "pending"
        with self.assertRaises(RetryLater):
            reviewer.finish()
        resumed = FakeWorkflow(self.config, "reviewer", self.remote)
        self.assertTrue(resumed.prepare()["pending"])
        self.remote["ci"] = "success"
        resumed.finish()
        self.assertEqual(1, len(self.remote["reviews"]))

    def test_commit_and_pr_recovery_does_not_duplicate_remote_operations(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        (Path(context["cwd"]) / "app.txt").write_text("implemented\n")
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "Verified", "files": ["app.txt"], "commitMessage": "Implement"})
        original = worker.command
        with patch.object(worker, "command", side_effect=lambda name, extra=None: (_ for _ in ()).throw(RetryLater("Wacha down")) if name == "complete_task" else original(name, extra)):
            with self.assertRaises(RetryLater):
                worker.finish()
        sha = worker.state["commitSha"]
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        self.assertTrue(resumed.prepare()["pending"])
        resumed.finish()
        self.assertEqual(1, len(self.remote["prs"]))
        self.assertEqual(sha, run("git", "rev-parse", "HEAD", cwd=Path(context["cwd"])))

    def test_sha_change_after_review_prevents_merge(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed"})
        self.remote["prs"][1]["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(WorkflowError, "head changed"):
            reviewer.finish()
        self.assertFalse(self.remote["prs"][1]["merged"])

    def test_manifest_cannot_commit_runner_files_or_escape_worktree(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "Verified", "files": ["../secret"], "commitMessage": "Implement"})
        with self.assertRaisesRegex(WorkflowError, "manifest"):
            worker.finish()

    def test_reviewer_cannot_approve_modified_checkout(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        (Path(context["cwd"]) / "app.txt").write_text("unreviewed change\n")
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed"})
        with self.assertRaisesRegex(WorkflowError, "modified tracked"):
            reviewer.finish()

    def test_origin_must_match_wacha_repository(self):
        workflow = Workflow(self.config, "worker")
        workflow.workflow = {"repository": "org/repo"}
        with self.assertRaisesRegex(WorkflowError, "origin"):
            workflow.validate_remote()

    def test_reset_parser_handles_codex_and_claude_with_timezones(self):
        local_now = datetime(2026, 10, 8, 17, 37).astimezone()
        self.assertEqual(180, retry_seconds("try again at 5:39 PM", local_now))
        now = datetime(2026, 10, 8, 17, 37, tzinfo=ZoneInfo("Asia/Tokyo"))
        self.assertEqual(180, retry_seconds("resets 5:39pm (Asia/Tokyo)", now))

    def test_large_sse_response_does_not_write_to_a_closed_jq_pipe(self):
        path = self.base / "response.txt"
        value = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"payload": "x" * 200000}}}
        path.write_text(f"event: message\ndata: {json.dumps(value)}\n\n")
        script = 'set -euo pipefail; RALPH_CONFIG_PATH="$1"; source "$2"; normalize_mcp_response "$(cat "$3")"'
        result = subprocess.run(["bash", "-c", script, "bash", str(self.config), str(ROOT / "ralph/backends/wacha.sh"), str(path)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(value, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
