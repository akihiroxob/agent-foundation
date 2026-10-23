from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ralph.lib.execution_status import filter_summary, status_lines, brief_reason, accepted_changes


class ExecutionStatusTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {"roles": {role: {"agentName": role} for role in ("worker", "reviewer", "manager")}}
        self.record = self.root / '.ralph/github-state/manager/deferred/manager-c.json'
        self.record.parent.mkdir(parents=True)
        self.record.write_text(json.dumps({"deferred": True, "role": "manager", "principal": "manager", "projectId": "p",
            "task": {"id": "blocked", "title": "Dependent Task"}, "deferredResumeGeneration": 0,
            "deferredReason": "### ACとの対応\n\nlong evidence\n\n### 不足・修正条件\n\n- 前提Taskの受入待ちです。\n"}))

    def tearDown(self):
        self.directory.cleanup()

    def test_deferred_task_is_not_runnable_but_other_tasks_remain(self):
        summary = filter_summary(self.root, self.config, 'manager', {"projectId":"p", "availableCount":2,
            "tasks":[{"id":"blocked","status":"wait_accept"},{"id":"ready","status":"wait_accept"}]})
        self.assertEqual(summary['availableCount'],1)
        self.assertEqual(summary['deferredCount'],1)
        self.assertEqual(summary['tasks'][0]['id'],'ready')
        self.assertEqual(summary['deferred'][0]['reason'],'前提Taskの受入待ちです。')
        self.assertEqual(summary['deferred'][0]['recordPath'],str(self.record))

    def test_explicit_resume_makes_own_task_eligible_for_restoration(self):
        (self.root/'.ralph/runtime.json').write_text(json.dumps({"agents":{"manager":{"manager":{"resumeGeneration":1}}}}))
        summary=filter_summary(self.root,self.config,'manager',{"projectId":"p","availableCount":1,"tasks":[{"id":"blocked","status":"wait_accept"}]})
        self.assertEqual(summary['availableCount'],1)
        self.assertEqual(summary['deferredCount'],0)

    def test_explicit_resume_restores_execution_hidden_by_its_existing_claim(self):
        (self.root / '.ralph/runtime.json').write_text(json.dumps({"agents": {"manager": {"manager": {"resumeGeneration": 1}}}}))
        summary = filter_summary(self.root, self.config, "manager", {"projectId": "p", "availableCount": 0, "tasks": []})
        self.assertEqual(summary["availableCount"], 1)
        self.assertEqual(summary["remoteAvailableCount"], 0)
        self.assertEqual(summary["tasks"][0]["id"], "blocked")

    def test_other_project_deferral_does_not_block_current_project(self):
        summary=filter_summary(self.root,self.config,'manager',{"projectId":"other","availableCount":1,"tasks":[{"id":"blocked","status":"wait_accept"}]})
        self.assertEqual(summary['availableCount'],1)
        self.assertEqual(summary['deferredCount'],0)

    def test_accepted_updates_wake_only_the_owner_and_only_safe_blocked_results(self):
        record = json.loads(self.record.read_text())
        record.update(deferredAt=100, releasedClaim=True, pendingResult={"decision": "blocked"})
        self.record.write_text(json.dumps(record))
        accepted = [{"id": "prerequisite", "projectId": "p", "status": "accepted", "updatedAt": 101}]
        summary = {"projectId": "p", "tasks": [{"id": "blocked", "status": "wait_accept"}]}
        self.assertEqual(filter_summary(self.root, self.config, "manager", summary, accepted)["availableCount"], 1)
        for change in ({"principal": "someone-else"}, {"releasedClaim": False},
                {"operationRecovery": {"reason": "uncertain"}}, {"finalCommand": {"name": "release_claim"}},
                {"prCreation": {"outcome": "unknown"}}, {"pendingResult": {"decision": "approve"}}):
            with self.subTest(change=change):
                self.record.write_text(json.dumps({**record, **change}))
                self.assertEqual(filter_summary(self.root, self.config, "manager", summary, accepted)["availableCount"], 0)

    def test_old_other_project_or_own_acceptance_does_not_wake_a_task(self):
        record = json.loads(self.record.read_text())
        record.update(deferredAt=100, releasedClaim=True, pendingResult={"decision": "blocked"})
        for task in ({"id": "dep", "projectId": "p", "status": "accepted", "updatedAt": 100},
                {"id": "dep", "projectId": "other", "status": "accepted", "updatedAt": 101},
                {"id": "blocked", "projectId": "p", "status": "accepted", "updatedAt": 101}):
            self.assertEqual(accepted_changes(record, [task]), [])

    def test_status_reports_runner_stopped_instead_of_trusting_stale_activity(self):
        (self.root/'.ralph/runtime.json').write_text(json.dumps({"agents":{"manager":{"manager":{"paused":False,"activity":{"phase":"agent_running","pid":1}}}}}))
        before=(self.root/'.ralph/runtime.json').read_bytes()
        output='\n'.join(status_lines(self.root,self.config))
        self.assertIn('manager:manager Runner停止',output)
        self.assertIn('保留=1',output)
        self.assertIn('前提Taskの受入待ち',output)
        self.assertEqual((self.root/'.ralph/runtime.json').read_bytes(),before)
        self.assertFalse((self.root/'.ralph/runtime.lock').exists())
