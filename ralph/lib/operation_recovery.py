"""Persist bounded Agent recovery decisions; never authorize a remote write here."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path


class RecoveryDeferred(Exception):
    pass


class OperationRecovery:
    def recovery_limits(self):
        if __package__:
            from .github_workflow import WorkflowError
        else:
            from github_workflow import WorkflowError
        retry = self.config.get("retry", {})
        if not isinstance(retry, dict) or not isinstance(retry.get("recovery", {}), dict):
            raise WorkflowError("retry.recovery must be an object")
        settings = retry.get("recovery", {})
        attempts, seconds = settings.get("maxAttempts", 3), settings.get("maxSeconds", 900)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (attempts, seconds)):
            raise WorkflowError("retry.recovery.maxAttempts and maxSeconds must be positive integers")
        return attempts, seconds

    def resume_generation(self):
        path = self.root / ".ralph/runtime.json"
        if not path.exists():
            return 0
        state = json.loads(path.read_text())
        return state.get("agents", {}).get(self.principal, {}).get(self.role, {}).get("resumeGeneration", 0)

    def recovery_remaining(self):
        _, seconds = self.recovery_limits()
        budget = self.state.get("recoveryBudget")
        return max(0, int(seconds - (time.time() - budget["startedAt"]))) if budget else seconds

    def recovery_cwd(self):
        if __package__:
            from .github_workflow import run, WorkflowError
        else:
            from github_workflow import run, WorkflowError
        cwd = Path(self.state["cwd"])
        if cwd != self.expected_worktree(self.state) or cwd.is_symlink() or cwd.resolve() != cwd.absolute() or not cwd.is_dir():
            raise WorkflowError("Recovery requires the owned execution Worktree")
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if f"worktree {cwd}" not in registered:
            raise WorkflowError("Recovery Worktree is not registered")
        return cwd

    def diagnostic_snapshot(self):
        if __package__:
            from .github_workflow import run
            from .host_verification import fingerprint
        else:
            from github_workflow import run
            from host_verification import fingerprint
        cwd = self.recovery_cwd()
        result = Path(self.state["resultPath"])
        return {"headSha": run("git", "rev-parse", "HEAD", cwd=cwd), "source": fingerprint(cwd),
            "index": hashlib.sha256(run("git", "diff", "--cached", "--binary", cwd=cwd).encode()).hexdigest(),
            "result": hashlib.sha256(result.read_bytes()).hexdigest() if result.is_file() and not result.is_symlink() else None}

    def reserve_recovery_budget(self, stage, reason):
        if __package__:
            from .github_workflow import run
            from .host_verification import fingerprint
        else:
            from github_workflow import run
            from host_verification import fingerprint
        cwd = self.recovery_cwd()
        limit, _ = self.recovery_limits()
        budget = self.state.setdefault("recoveryBudget", {"startedAt": time.time(), "attempts": 0})
        result_path = Path(self.state.get("resultPath", cwd / ".ralph-result.json"))
        result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path.is_file() and not result_path.is_symlink() else None
        signature = hashlib.sha256(json.dumps([stage, self.phase, reason,
            run("git", "rev-parse", "HEAD", cwd=cwd), fingerprint(cwd), result_hash,
            self.state.get("pendingResult")], sort_keys=True).encode()).hexdigest()
        if budget["attempts"] >= limit or self.recovery_remaining() == 0 or budget.get("lastFailure") == signature:
            self.defer_execution("Recovery budget exhausted or the same failure repeated without progress: " + reason)
            raise RecoveryDeferred(reason)
        budget["attempts"] += 1
        budget["lastFailure"] = signature
        self.save()
        return budget["attempts"], limit

    def schedule_operation_recovery(self, stage, reason):
        if __package__:
            from .github_workflow import atomic_json
        else:
            from github_workflow import atomic_json
        cwd = self.recovery_cwd()
        attempt, limit = self.reserve_recovery_budget(stage, reason)
        directory = self.root / ".ralph/verification" / self.state.get("worktreeClaimId", self.state["claimId"]) / "operation-recovery" / str(attempt)
        atomic_json(directory / "execution.json", self.state)
        if "pendingResult" in self.state:
            atomic_json(directory / "result.json", self.state["pendingResult"])
        result_path = cwd / ".ralph/recovery-result.json"
        result_path.unlink(missing_ok=True)
        self.state["operationRecovery"] = {"stage": stage, "phase": self.phase, "reason": reason,
            "attempt": attempt, "limit": limit, "workflow": self.workflow,
            "previousExecutionPath": str(directory / "execution.json"),
            "previousResultPath": str(directory / "result.json") if "pendingResult" in self.state else None,
            "decisionPath": str(result_path), "recordDirectory": str(directory), "readOnlySnapshot": self.diagnostic_snapshot()}
        self.state["executionError"] = {"phase": stage + "/" + self.phase, "reason": reason}
        self.save()

    def recovery_context(self):
        if __package__:
            from .github_workflow import WorkflowError
        else:
            from github_workflow import WorkflowError
        cwd = self.recovery_cwd()
        if not self.recovery_remaining():
            self.defer_execution("Recovery elapsed time limit reached")
            raise RecoveryDeferred("Recovery elapsed time limit reached")
        recovery = self.state["operationRecovery"]
        return {**self.state, "pending": False, "recovery": True, "cwd": str(cwd),
            "resultPath": recovery["decisionPath"], "recoveryTimeoutSeconds": self.recovery_remaining()}

    def apply_recovery(self):
        if __package__:
            from .github_workflow import run, atomic_json, WorkflowError
        else:
            from github_workflow import run, atomic_json, WorkflowError
        recovery = self.state["operationRecovery"]
        cwd = self.recovery_cwd()
        path = Path(recovery["decisionPath"])
        if path != cwd / ".ralph/recovery-result.json":
            raise WorkflowError("Recovery decision path is not inside the owned Worktree")
        if self.recovery_remaining() == 0:
            self.defer_execution("Recovery elapsed time limit reached")
            raise RecoveryDeferred("Recovery elapsed time limit reached")
        if path.is_symlink() or not path.is_file():
            raise WorkflowError("Recovery Agent did not write its decision file")
        if self.diagnostic_snapshot() != recovery["readOnlySnapshot"]:
            self.defer_execution("Recovery diagnosis modified HEAD/index/source/Role result; maintenance must reconcile the preserved evidence")
            raise RecoveryDeferred("Recovery diagnosis modified protected execution state")
        decision = json.loads(path.read_text())
        if not isinstance(decision, dict) or decision.get("action") not in {"retry", "revise", "defer"} or not isinstance(decision.get("summary"), str) or not decision["summary"].strip():
            raise WorkflowError("Recovery decision requires action retry/revise/defer and a non-empty summary")
        atomic_json(Path(recovery["recordDirectory"]) / "decision.json", decision)
        if decision["action"] == "defer":
            self.defer_execution(decision["summary"])
            raise RecoveryDeferred(decision["summary"])
        if decision["action"] == "revise":
            # Once an irreversible operation may have happened, preserve its exact request/result.
            if any(self.state.get(key) for key in ["commitSha", "prNumber", "reviewCommentId", "finalCommand"]):
                raise WorkflowError("Cannot revise a decision after commit/PR/final operation; use retry for reconciliation or defer")
            if self.role == "worker":
                head = run("git", "rev-parse", "HEAD", cwd=cwd)
                if head != self.state["initialHeadSha"] and not self.adopt_worker_base(cwd, head):
                    raise WorkflowError("Cannot revise after HEAD changed outside the remote base; reconcile the saved commit first")
            self.state.pop("pendingResult", None)
        path.unlink()
        self.state.pop("operationRecovery")
        self.save()

    def deferred_records(self):
        for path in (self.root / ".ralph/github-state").glob("*/deferred/*.json"):
            record = json.loads(path.read_text())
            if record.get("deferred") and record.get("role") == self.role:
                yield path, record

    def defer_execution(self, reason):
        if __package__:
            from .github_workflow import run, atomic_json, WorkflowError
        else:
            from github_workflow import run, atomic_json, WorkflowError
        cwd = self.recovery_cwd()
        destination = cwd.parent / "deferred" / f"{self.role}-{self.state['claimId']}"
        if destination.exists() or destination.is_symlink() or destination.resolve() != destination.absolute():
            raise WorkflowError("Deferred destination already exists; execution retained")
        self.state.update(deferred=True, deferredReason=reason, deferredAt=int(time.time() * 1000),
            deferredResumeGeneration=self.resume_generation(), deferredDestination=str(destination))
        archive = self.state_path.parent / "deferred" / f"{self.role}-{self.state['claimId']}.json"
        # Journal before moving, so a crash can be retried without losing the Worktree.
        self.save()
        atomic_json(archive, self.state)
        self.finish_deferral()

    def finish_deferral(self):
        if __package__:
            from .github_workflow import run, atomic_json, WorkflowError
        else:
            from github_workflow import run, atomic_json, WorkflowError
        expected = self.expected_worktree(self.state)
        destination = expected.parent / "deferred" / f"{self.role}-{self.state['claimId']}"
        if self.state.get("deferredDestination") != str(destination) or destination.is_symlink() or expected.is_symlink() or destination.resolve() != destination.absolute() or expected.resolve() != expected.absolute():
            raise WorkflowError("Deferred Worktree path does not match execution ownership")
        if expected.exists() and not destination.exists():
            self.recovery_cwd()
            destination.parent.mkdir(parents=True, exist_ok=True)
            run("git", "worktree", "move", str(expected), str(destination), cwd=self.root)
        elif expected.exists() or not destination.is_dir():
            raise WorkflowError("Cannot reconcile deferred Worktree move; execution retained")
        registered = run("git", "worktree", "list", "--porcelain", "-z", cwd=self.root).split("\0")
        if f"worktree {destination}" not in registered:
            raise WorkflowError("Deferred Worktree is not registered")
        self.state["cwd"] = str(destination)
        self.state["resultPath"] = str(destination / ".ralph-result.json")
        archive = self.state_path.parent / "deferred" / f"{self.role}-{self.state['claimId']}.json"
        atomic_json(archive, self.state)
        # Do not release an uncertain Claim or resend a remote mutation when parking work.
        self.clear()

    def restore_deferred(self):
        if __package__:
            from .execution_status import accepted_tasks, accepted_changes
        else:
            from execution_status import accepted_tasks, accepted_changes
        records = [(path, record) for path, record in self.deferred_records() if record["principal"] == self.principal]
        accepted = None
        for path, record in records:
            explicit = self.resume_generation() > record.get("deferredResumeGeneration", 0)
            changes = []
            if not explicit:
                if accepted is None:
                    accepted = accepted_tasks(self, [item for _, item in records])
                changes = accepted_changes(record, accepted)
                if not changes:
                    continue
            self.state = {**record, "restorePending": True}
            if not explicit:
                self.state["automaticResume"] = {"acceptedTasks": changes, "deferredAt": record["deferredAt"],
                    "instruction": "別TaskのAccepted更新を検知したため再評価する。保存された保留理由と現在の前提を照合し、解消を推測せず判断する。"}
            else:
                self.state.pop("automaticResume", None)
            self.save()
            self.finish_restore()
            if changes:
                print("保留Taskを自動再評価します。Task=" + " ".join(record["task"].get("title", record["task"]["id"]).split())
                    + " [" + record["task"]["id"] + "] Accepted更新=" + ",".join(item["id"] for item in changes), file=sys.stderr, flush=True)
            return

    def finish_restore(self):
        if __package__:
            from .github_workflow import run, atomic_json, WorkflowError
        else:
            from github_workflow import run, atomic_json, WorkflowError
        destination = Path(self.state["deferredDestination"])
        expected = self.expected_worktree(self.state)
        if destination != expected.parent / "deferred" / f"{self.role}-{self.state['claimId']}" or destination.is_symlink() or expected.is_symlink() or destination.resolve() != destination.absolute() or expected.resolve() != expected.absolute():
            raise WorkflowError("Cannot restore redirected deferred Worktree")
        if destination.exists() and not expected.exists():
            run("git", "worktree", "move", str(destination), str(expected), cwd=self.root)
        elif destination.exists() or not expected.is_dir():
            raise WorkflowError("Cannot reconcile deferred Worktree restore; execution retained")
        self.state.update(cwd=str(expected), resultPath=str(expected / ".ralph-result.json"), deferred=False)
        self.recovery_cwd()
        if self.state.get("pendingResult", {}).get("decision") == "blocked":
            automatic = self.state.get("automaticResume")
            evidence_key = "automatic-" + str(automatic["deferredAt"]) if automatic else str(self.resume_generation())
            evidence = (self.root / ".ralph/verification" / self.state.get("worktreeClaimId", self.state["claimId"])
                / "deferred-resume" / evidence_key)
            atomic_json(evidence / "execution.json", self.state)
            atomic_json(evidence / "result.json", self.state["pendingResult"])
            self.state["resumedBlockerPath"] = str(evidence / "result.json")
            self.state.pop("pendingResult")
            self.state["releasedResume"] = {}
        self.state.pop("recoveryBudget", None)
        self.state.pop("operationRecovery", None)
        (expected / ".ralph/recovery-result.json").unlink(missing_ok=True)
        path = self.state_path.parent / "deferred" / f"{self.role}-{self.state['claimId']}.json"
        atomic_json(path, self.state)
        self.state.pop("restorePending", None)
        self.save()
