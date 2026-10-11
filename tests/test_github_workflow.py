from __future__ import annotations

import json
import io
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ralph.lib.github_workflow import Workflow, WorkflowError, RetryLater, atomic_json, run, main
from ralph.lib.limit_reset import retry_seconds

ROOT = Path(__file__).resolve().parents[1]


class FakeWorkflow(Workflow):
    def __init__(self, config, role, remote):
        super().__init__(config, role)
        self.remote = remote
        self.commands = []

    def validate_remote(self):
        pass  # Real local bare repository substitutes for github.com in this test.

    def git_remote(self, operation, refspec, cwd):
        return run("git", operation, "origin", refspec, cwd=cwd)

    def mcp(self, name, arguments):
        self.commands.append((name, arguments))
        state = self.remote
        if name == "list_projects":
            return {"projects": [{"id": "project", "name": "sample", "workflow": {
                "repository": "org/repo", "baseBranch": "main", "requiredChecks": state.get("requiredChecks", ["verify"])}}]}
        if name == "list_tasks":
            if "status" in arguments["filter"]:
                return {"tasks": [state["task"]] if state["task"]["status"] in arguments["filter"]["status"] else []}
            phase = arguments["filter"]["availableFor"]
            expected = {"work": ["todo", "rejected"], "review": ["in_review"], "acceptance": ["wait_accept"]}[phase]
            expired = state.get("currentClaim") in state.get("expiredClaims", set())
            if phase == "work" and expired:
                expected += ["doing"]
            return {"tasks": [{**state["task"], "reclaimable": expired}] if state["task"]["status"] in expected else []}
        if name.startswith("claim_"):
            if arguments["requestId"] in state["receipts"]:
                return state["receipts"][arguments["requestId"]]
            if state.get("claimConflict"):
                raise RetryLater("Wacha claim: CLAIM_CONFLICT")
            state["claims"] += 1
            if name == "claim_task":
                state["task"]["status"] = "doing"
            state["currentClaim"] = f"claim-{state['claims']}"
            receipt = {"claimId": state["currentClaim"]}
            state["receipts"][arguments["requestId"]] = receipt
            if state.pop("loseClaimResponse", False):
                raise RetryLater("Wacha unavailable after Claim acquisition")
            return receipt
        if name == "list_task_pull_requests":
            return {"pullRequests": state["history"]}
        if name == "renew_claim":
            if arguments["claimId"] in state.get("expiredClaims", set()):
                raise WorkflowError("Wacha renew_claim: CLAIM_EXPIRED")
            return {}
        key = arguments["requestId"]
        if key in state["receipts"]:
            return state["receipts"][key]
        if arguments.get("claimId") in state.get("expiredClaims", set()):
            raise WorkflowError(f"Wacha {name}: CLAIM_EXPIRED")
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
            pr = {"number": number, "body": body["body"], "head": {"sha": sha, "ref": body["head"], "repo": {"full_name": "org/repo"}},
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

    def worker(self, text="implemented\n", summary="AC verified, tests passed"):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        (Path(context["cwd"]) / "app.txt").write_text(text)
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": summary, "files": ["app.txt"], "commitMessage": "Implement Task"})
        worker.finish()
        return worker, context

    def test_pr_and_review_preserve_markdown_and_review_receipt(self):
        summary = "### 変更内容\n\n- `app.txt` を更新\n\n### 検証結果\n\n- **成功**\n- 未検証事項も記載"
        self.worker(summary=summary)
        pr = self.remote["prs"][1]
        self.assertEqual(pr["body"], f"## 実装報告\n\n{summary}\n\n---\n\n- Wacha Task: `task-1`")
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        reviewer.prepare()
        for decision in ("approve", "reject"):
            comment_id = reviewer.post_review_comment("/repos/org/repo/pulls/1", pr["head"]["sha"], decision, summary)
            self.assertEqual(comment_id, reviewer.post_review_comment("/repos/org/repo/pulls/1", pr["head"]["sha"], decision, summary))
            body = self.remote["reviews"][-1]["body"]
            self.assertTrue(body.startswith(f"## レビュー結果: **{decision}**\n\n{summary}\n\n"))
            self.assertIn("\n- Reviewer: reviewer\n- Task: `task-1`\n- Head: `", body)
            receipt = json.loads(body.split("<!-- task-review: ", 1)[1].split(" -->", 1)[0])
            self.assertEqual(receipt, {"taskId": "task-1", "claimId": reviewer.state["claimId"],
                "headSha": pr["head"]["sha"], "decision": decision})
        self.assertEqual(len(self.remote["reviews"]), 2)

    def finish_through_cli(self, workflow):
        with patch("ralph.lib.github_workflow.Workflow", return_value=workflow), patch("sys.argv", [
                "github_workflow.py", "finish", "--config", str(self.config), "--role", workflow.role]), \
                patch("sys.stderr", new_callable=io.StringIO) as errors:
            status = main()
        return status, errors.getvalue()

    def test_missing_result_retains_claim_and_changes_and_resumes_same_worktree(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        worktree = Path(context["cwd"])
        (worktree / "app.txt").write_text("unfinished implementation\n")
        (worktree / "new-file.txt").write_text("new implementation\n")
        status, error = self.finish_through_cli(worker)
        self.assertEqual(status, 78)
        self.assertIn("Agent did not write a decision file", error)
        saved = json.loads(worker.state_path.read_text())
        self.assertEqual(saved["claimId"], context["claimId"])
        self.assertEqual(saved["executionError"]["phase"], "finish")
        self.assertEqual(self.remote["task"]["status"], "doing")
        self.assertFalse(any(name == "release_claim" for name, _ in worker.commands))
        self.assertFalse(self.remote["prs"])
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        next_context = resumed.prepare()
        self.assertEqual(next_context["cwd"], context["cwd"])
        self.assertEqual(next_context["claimId"], context["claimId"])
        self.assertEqual(self.remote["claims"], 1)
        self.assertEqual((worktree / "app.txt").read_text(), "unfinished implementation\n")
        self.assertEqual((worktree / "new-file.txt").read_text(), "new implementation\n")
        self.assertIn("executionError", next_context)
        resumed.release_on_error("Wacha renew_claim: CLAIM_EXPIRED")
        self.assertTrue(resumed.state_path.exists())
        self.assertFalse(any(name == "release_claim" for name, _ in resumed.commands))
        atomic_json(worktree / ".ralph-result.json", {"decision": "complete", "summary": "Revalidated all changes",
            "files": ["app.txt", "new-file.txt"], "commitMessage": "Resume implementation"})
        resumed.finish()
        self.assertEqual(self.remote["task"]["status"], "in_review")
        self.assertFalse(resumed.state_path.exists())
        self.assertFalse(worktree.exists())

    def test_invalid_result_is_retained_without_releasing_claim(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        path = Path(context["resultPath"])
        for text in ("{not valid json", '{"decision":"complete","summary":""}'):
            path.write_text(text)
            status, _ = self.finish_through_cli(worker)
            self.assertEqual(status, 78)
            self.assertTrue(worker.state_path.exists())
            self.assertEqual(path.read_text(), text)
            self.assertIn("executionError", json.loads(worker.state_path.read_text()))
            self.assertEqual(self.remote["task"]["status"], "doing")
            self.assertFalse(any(name == "release_claim" for name, _ in worker.commands))

    def test_cli_blocked_result_does_not_recreate_released_execution(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "blocked", "summary": "Prerequisite is in review"})
        status, error = self.finish_through_cli(worker)
        self.assertEqual(status, 78)
        self.assertIn("Agent reported an environment blocker", error)
        self.assertFalse(worker.state_path.exists())
        self.assertEqual(self.remote["task"]["status"], "todo")
        self.assertEqual(sum(name == "release_claim" for name, _ in worker.commands), 1)
        self.assertTrue(Path(context["cwd"]).exists())

    def test_expired_worker_claim_reacquires_same_task_and_preserves_worktree(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        directory = Path(context["cwd"])
        (directory / "app.txt").write_text("preserved implementation\n")
        self.remote["expiredClaims"] = {context["claimId"]}
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        next_context = resumed.prepare()
        self.assertNotEqual(next_context["claimId"], context["claimId"])
        self.assertEqual(next_context["cwd"], context["cwd"])
        self.assertEqual(next_context["branch"], context["branch"])
        self.assertEqual((directory / "app.txt").read_text(), "preserved implementation\n")
        atomic_json(directory / ".ralph-result.json", {"decision": "complete", "summary": "Verified preserved work",
            "files": ["app.txt"], "commitMessage": "Recover implementation"})
        resumed.finish()
        self.assertFalse(directory.exists(), "Cleanup must recognize the original Worktree Claim")
        self.assertEqual(self.remote["task"]["status"], "in_review")

    def test_reclaim_lost_response_reuses_receipt_and_conflict_keeps_execution(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        self.remote["expiredClaims"] = {context["claimId"]}
        self.remote["claimConflict"] = True
        with self.assertRaises(RetryLater):
            worker.prepare()
        worker.release_on_error("Wacha claim: CLAIM_CONFLICT")
        self.assertTrue(worker.state_path.exists())
        request_id = worker.state["claimRecovery"]["requestId"]
        self.remote.pop("claimConflict")
        self.remote["loseClaimResponse"] = True
        with self.assertRaises(RetryLater):
            worker.prepare()
        recovered = FakeWorkflow(self.config, "worker", self.remote)
        next_context = recovered.prepare()
        self.assertEqual(self.remote["claims"], 2)
        self.assertEqual(next_context["cwd"], context["cwd"])
        self.assertEqual(next_context["claimId"], self.remote["receipts"][request_id]["claimId"])

    def test_expiry_after_phase_change_does_not_reclaim_or_discard_changes(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        self.remote["expiredClaims"] = {context["claimId"]}
        self.remote["task"]["status"] = "in_review"
        with self.assertRaisesRegex(WorkflowError, "execution retained"):
            worker.prepare()
        worker.release_on_error("Expired Claim cannot be reacquired; execution retained")
        self.assertTrue(worker.state_path.exists())
        self.assertTrue(Path(context["cwd"]).exists())
        self.assertEqual(self.remote["claims"], 1)
        self.remote["task"]["status"] = "doing"
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        self.remote["task"]["description"] = "Requirements changed while Claim expired"
        with self.assertRaisesRegex(WorkflowError, "requirements changed"):
            resumed.prepare()
        self.assertTrue(resumed.state_path.exists())
        self.assertEqual(self.remote["claims"], 1)

    def test_expired_review_and_acceptance_rebind_evidence_to_new_claim(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed"})
        original = reviewer.mcp

        def expire_before_review(name, arguments):
            if name == "reviewed_task":
                self.remote.setdefault("expiredClaims", set()).add(context["claimId"])
            return original(name, arguments)

        with patch.object(reviewer, "mcp", side_effect=expire_before_review):
            with self.assertRaisesRegex(WorkflowError, "CLAIM_EXPIRED"):
                reviewer.finish()
        self.assertTrue(self.remote["prs"][1]["merged"])
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        reviewer.finish()
        self.assertEqual(self.remote["task"]["status"], "wait_accept")
        self.assertEqual(len(self.remote["reviews"]), 2)
        receipt = json.loads(self.remote["reviews"][-1]["body"].split("<!-- task-review: ")[1].split(" -->")[0])
        self.assertNotEqual(receipt["claimId"], context["claimId"])
        self.assertFalse(Path(context["cwd"]).exists())
        manager = FakeWorkflow(self.config, "manager", self.remote)
        context = manager.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "accept", "summary": "Integrated AC verified"})
        self.remote["expiredClaims"].add(context["claimId"])
        manager.finish()
        self.assertEqual(self.remote["task"]["status"], "accepted")
        self.assertFalse(Path(context["cwd"]).exists())

    def test_worktree_pr_review_merge_accept_and_new_pr_after_manager_rejection(self):
        _, context = self.worker()
        self.assertEqual("initial\n", (self.repo / "app.txt").read_text(), "main checkout must remain isolated")
        self.assertEqual("in_review", self.remote["task"]["status"])
        self.assertFalse(Path(context["cwd"]).exists())
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        review = reviewer.prepare()
        atomic_json(Path(review["resultPath"]), {"decision": "approve", "summary": "Diff and tests reviewed"})
        reviewer.finish()
        self.assertEqual("wait_accept", self.remote["task"]["status"])
        self.assertFalse(Path(review["cwd"]).exists())
        manager = FakeWorkflow(self.config, "manager", self.remote)
        acceptance = manager.prepare()
        self.assertEqual(acceptance["expectedMergeSha"], run("git", "rev-parse", "HEAD", cwd=Path(acceptance["cwd"])))
        atomic_json(Path(acceptance["resultPath"]), {"decision": "reject", "summary": "Integrated AC still missing"})
        manager.finish()
        self.assertFalse(Path(acceptance["cwd"]).exists())
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
        self.assertFalse(Path(acceptance["cwd"]).exists())
        self.assertEqual(acceptance["expectedMergeSha"], self.remote["verification"]["mergeSha"])

    def test_reviewer_rejection_reuses_same_pr_and_records_named_role_comment(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "reject", "summary": "Missing regression test"})
        reviewer.finish()
        self.assertEqual("rejected", self.remote["task"]["status"])
        self.assertFalse(Path(context["cwd"]).exists())
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
        self.assertTrue(Path(context["cwd"]).exists())
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
        self.assertTrue(Path(context["cwd"]).exists())
        sha = worker.state["commitSha"]
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        self.assertTrue(resumed.prepare()["pending"])
        resumed.finish()
        self.assertEqual(1, len(self.remote["prs"]))
        self.assertFalse(Path(context["cwd"]).exists())
        self.assertEqual(sha, self.remote["prs"][1]["head"]["sha"])

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

    def manifest_worker(self, files):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "Verified",
            "files": files, "commitMessage": "Update files"})
        return worker, Path(context["cwd"])

    def test_staged_rename_commits_latest_destination_content(self):
        worker, cwd = self.manifest_worker(["app.txt", "renamed app.txt"])
        run("git", "mv", "app.txt", "renamed app.txt", cwd=cwd)
        (cwd / "renamed app.txt").write_text("updated after rename\n")
        worker.finish()
        sha = self.remote["prs"][1]["head"]["sha"]
        self.assertEqual("updated after rename", run("git", "show", f"{sha}:renamed app.txt", cwd=self.remote["bare"]))
        changed = set(run("git", "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", sha,
            cwd=self.remote["bare"]).splitlines())
        self.assertEqual({"app.txt", "renamed app.txt"}, changed)
        self.assertEqual("in_review", self.remote["task"]["status"])

    def test_staged_deletion_only_commits_without_git_add(self):
        worker, cwd = self.manifest_worker(["app.txt"])
        run("git", "rm", "app.txt", cwd=cwd)
        worker.finish()
        sha = self.remote["prs"][1]["head"]["sha"]
        self.assertNotIn("app.txt", run("git", "ls-tree", "--name-only", sha, cwd=self.remote["bare"]).splitlines())

    def test_unstaged_deletion_is_staged_and_committed(self):
        worker, cwd = self.manifest_worker(["app.txt"])
        (cwd / "app.txt").unlink()
        worker.finish()
        self.assertEqual("in_review", self.remote["task"]["status"])

    def test_unstaged_rename_is_staged_and_committed(self):
        worker, cwd = self.manifest_worker(["app.txt", "new.txt"])
        (cwd / "app.txt").rename(cwd / "new.txt")
        worker.finish()
        sha = self.remote["prs"][1]["head"]["sha"]
        self.assertEqual("initial", run("git", "show", f"{sha}:new.txt", cwd=self.remote["bare"]))

    def test_missing_manifest_path_is_rejected_before_staging(self):
        worker, cwd = self.manifest_worker(["app.txt", "typo.txt"])
        (cwd / "app.txt").write_text("changed\n")
        with self.assertRaisesRegex(WorkflowError, "missing.*staged deletion"):
            worker.finish()
        self.assertEqual("", run("git", "diff", "--cached", "--name-only", cwd=cwd))
        self.assertFalse(self.remote["prs"])

    def test_staged_change_outside_manifest_is_rejected(self):
        worker, cwd = self.manifest_worker(["app.txt"])
        (cwd / "app.txt").write_text("changed\n")
        (cwd / ".gitignore").write_text(".ralph/\n.ralph-result.json\nother/\n")
        run("git", "add", ".gitignore", cwd=cwd)
        with self.assertRaisesRegex(WorkflowError, "Staged paths"):
            worker.finish()
        self.assertFalse(self.remote["prs"])
        self.assertEqual(worker.state["initialHeadSha"], run("git", "rev-parse", "HEAD", cwd=cwd))

    def test_staged_rename_requires_source_in_manifest(self):
        worker, cwd = self.manifest_worker(["new.txt"])
        run("git", "mv", "app.txt", "new.txt", cwd=cwd)
        with self.assertRaisesRegex(WorkflowError, "Staged paths"):
            worker.finish()
        self.assertFalse(self.remote["prs"])

    def test_staged_rename_requires_destination_in_manifest(self):
        worker, cwd = self.manifest_worker(["app.txt"])
        run("git", "mv", "app.txt", "new.txt", cwd=cwd)
        with self.assertRaisesRegex(WorkflowError, "Staged paths"):
            worker.finish()
        self.assertFalse(self.remote["prs"])

    def test_reviewer_cannot_approve_modified_checkout(self):
        self.worker()
        reviewer = FakeWorkflow(self.config, "reviewer", self.remote)
        context = reviewer.prepare()
        (Path(context["cwd"]) / "app.txt").write_text("unreviewed change\n")
        atomic_json(Path(context["resultPath"]), {"decision": "approve", "summary": "Reviewed"})
        with self.assertRaisesRegex(WorkflowError, "modified tracked"):
            reviewer.finish()

    def test_cleanup_failure_does_not_repeat_task_and_retries_without_api_calls(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        (Path(context["cwd"]) / "app.txt").write_text("implemented\n")
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "Verified", "files": ["app.txt"], "commitMessage": "Implement"})
        with patch.object(worker, "cleanup_worktree", side_effect=WorkflowError("locked")):
            worker.finish()
        self.assertEqual("in_review", self.remote["task"]["status"])
        self.assertFalse(worker.state_path.exists())
        self.assertTrue(Path(context["cwd"]).exists())
        records = list((worker.state_path.parent / "completed").glob("worker-*.json"))
        self.assertEqual(1, len(records))
        self.assertEqual("retained", json.loads(records[0].read_text())["cleanup"])
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        with patch.object(resumed, "mcp", side_effect=AssertionError("No Wacha calls for cleanup")):
            resumed.retry_completed_cleanup()
        self.assertFalse(Path(context["cwd"]).exists())
        self.assertEqual("removed", json.loads(records[0].read_text())["cleanup"])
        self.assertEqual(1, len(self.remote["prs"]))

    def test_cleanup_keeps_dirty_untracked_unexpected_commits_and_outside_paths(self):
        worker, context = self.worker()
        execution = worker.state.copy()
        directory = Path(context["cwd"])
        run("git", "worktree", "add", "--detach", str(directory), execution["commitSha"], cwd=self.repo)
        (directory / "app.txt").write_text("unsaved\n")
        with self.assertRaisesRegex(WorkflowError, "uncommitted"):
            worker.cleanup_worktree(execution)
        (directory / "app.txt").write_text("implemented\n")
        (directory / "notes.txt").write_text("keep me\n")
        with self.assertRaisesRegex(WorkflowError, "untracked"):
            worker.cleanup_worktree(execution)
        (directory / "notes.txt").unlink()
        run("git", "commit", "--allow-empty", "-m", "Unsent", cwd=directory)
        with self.assertRaisesRegex(WorkflowError, "unexpected commit"):
            worker.cleanup_worktree(execution)
        with self.assertRaisesRegex(WorkflowError, "expected Ralph"):
            worker.cleanup_worktree({**execution, "cwd": str(self.repo)})
        self.assertTrue(directory.exists())

    def test_completed_execution_recovery_needs_no_network_or_claim(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        (Path(context["cwd"]) / "app.txt").write_text("implemented\n")
        atomic_json(Path(context["resultPath"]), {"decision": "complete", "summary": "Verified", "files": ["app.txt"], "commitMessage": "Implement"})
        with patch.object(worker, "cleanup_record", side_effect=OSError("Crash after archive")):
            with self.assertRaises(OSError):
                worker.finish()
        resumed = FakeWorkflow(self.config, "worker", self.remote)
        with patch.object(resumed, "mcp", side_effect=AssertionError("Completed execution needs no MCP")):
            self.assertTrue(resumed.prepare()["pending"])
            resumed.finish()
        self.assertFalse(Path(context["cwd"]).exists())
        self.assertFalse(resumed.state_path.exists())

    def test_blocked_execution_keeps_worktree(self):
        worker = FakeWorkflow(self.config, "worker", self.remote)
        context = worker.prepare()
        atomic_json(Path(context["resultPath"]), {"decision": "blocked", "summary": "Environment issue"})
        with self.assertRaisesRegex(WorkflowError, "environment blocker"):
            worker.finish()
        self.assertTrue(Path(context["cwd"]).exists())
        self.assertFalse((worker.state_path.parent / "completed").exists())

    def test_cleanup_retains_locked_checkout_and_unrecognized_result(self):
        worker, context = self.worker()
        directory = Path(context["cwd"])
        run("git", "worktree", "add", "--detach", str(directory), worker.state["commitSha"], cwd=self.repo)
        run("git", "worktree", "lock", str(directory), cwd=self.repo)
        with self.assertRaises(WorkflowError):
            worker.cleanup_worktree(worker.state)
        self.assertTrue(directory.exists())
        run("git", "worktree", "unlock", str(directory), cwd=self.repo)
        atomic_json(directory / ".ralph-result.json", {"unsaved": "retain this"})
        with self.assertRaisesRegex(WorkflowError, "changed after completion"):
            worker.cleanup_worktree(worker.state)
        self.assertTrue((directory / ".ralph-result.json").exists())
        with self.assertRaisesRegex(WorkflowError, "not successfully completed"):
            worker.cleanup_worktree({**worker.state, "completed": False})

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
