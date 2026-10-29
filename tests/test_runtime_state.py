from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ralph.lib.runtime_state import (acquire_roles, atomic_json, component, execution_path,
                                     migrate, operate, transaction)
from scripts.install_ralph import install_global

ROOT = Path(__file__).resolve().parents[1]


class RuntimeStateTest(unittest.TestCase):
    def test_identity_locks_and_partial_acquisition_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = acquire_roles(root, {"worker": "alice"})
            second = acquire_roles(root, {"worker": "bob", "reviewer": "alice"})
            try:
                with self.assertRaisesRegex(ValueError, "principal=alice role=worker"):
                    acquire_roles(root, {"manager": "alice", "worker": "alice"})
                rollback = acquire_roles(root, {"manager": "alice"})
                for descriptor in rollback:
                    os.close(descriptor)
            finally:
                for descriptor in first + second:
                    os.close(descriptor)
            fresh = acquire_roles(root, {"worker": "alice"})
            for descriptor in fresh:
                os.close(descriptor)

    def test_concurrent_state_writers_preserve_each_identity_and_quota_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = '''from pathlib import Path
import sys
from ralph.lib.runtime_state import operate
r=Path(sys.argv[1]); p=sys.argv[2]
for n in range(25):
 operate(r,p,'worker','no-progress')
 operate(r,p,'worker','set-quota',str(100+int(p)*10+n),'shared')
operate(r,p,'worker','pause','manual stop')
'''
            processes = [subprocess.Popen([sys.executable, "-c", script, directory, str(i)], cwd=ROOT)
                         for i in range(4)]
            for process in processes:
                self.assertEqual(0, process.wait(timeout=10))
            state = json.loads((root / ".ralph/runtime.json").read_text())
            self.assertEqual(154, state["quotas"]["shared"])
            for i in range(4):
                self.assertEqual(25, state["agents"][str(i)]["worker"]["attempts"])
                self.assertTrue(state["agents"][str(i)]["worker"]["paused"])
            operate(root, "0", "worker", "progress")
            self.assertTrue(operate(root, "0", "worker", "paused"))
            operate(root, "0", "worker", "resume")
            self.assertFalse(operate(root, "0", "worker", "paused"))
            self.assertTrue(operate(root, "1", "worker", "paused"))

    def test_migration_preserves_old_execution_pause_and_quota_without_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"roles": {"worker": {"agentName": "default-worker"}}}
            old = root / ".ralph/github-state/worker.json"
            atomic_json(old, {"claimId": "claim", "cwd": "original", "pendingResult": {"decision": "complete"}})
            atomic_json(root / ".ralph/pauses/worker.json", {"paused": True, "reason": "original cause"})
            atomic_json(root / ".ralph/quota.json", {"shared": 999})
            migrate(root, config)
            state = json.loads(execution_path(root, "default-worker", "worker").read_text())
            self.assertEqual("original", state["cwd"])
            self.assertTrue(state["legacyWorktree"])
            self.assertEqual("complete", state["pendingResult"]["decision"])
            self.assertTrue(old.with_suffix(".json.migrated").exists())
            self.assertTrue(operate(root, "default-worker", "worker", "paused"))
            operate(root, "default-worker", "worker", "resume")
            migrate(root, config)
            self.assertFalse(operate(root, "default-worker", "worker", "paused"))
            self.assertEqual(999, operate(root, "default-worker", "worker", "quota", key="shared"))

    def test_migration_refuses_live_legacy_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".ralph").mkdir()
            (root / ".ralph/runner.pid").write_text(str(os.getpid()))
            with self.assertRaisesRegex(ValueError, "Legacy Ralph is still running"):
                migrate(root, {"roles": {"worker": {"agentName": "worker"}}})
            self.assertFalse((root / ".ralph/runtime.json").exists())

    def test_corrupt_state_is_not_overwritten_and_unknown_fields_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with transaction(root) as state:
                state["extension"] = {"new": True}
            operate(root, "a", "worker", "resume")
            self.assertEqual({"new": True}, json.loads((root / ".ralph/runtime.json").read_text())["extension"])
            path = root / ".ralph/runtime.json"
            path.write_text('{"broken":')
            with self.assertRaises(ValueError):
                operate(root, "a", "worker", "pause", "stop")
            self.assertEqual('{"broken":', path.read_text())

    def test_principal_components_cannot_escape_namespace(self):
        self.assertNotEqual(component("Alice").casefold(), component("alice").casefold())
        for name in ("../other", "/tmp/other", "..", "日本語"):
            part = component(name)
            self.assertNotIn("/", part)
            self.assertNotEqual("..", part)
        for name in ("", " a", "a\n", "a\x7f"):
            with self.assertRaises(ValueError):
                component(name)

    def test_cli_runs_two_workers_and_another_role_but_rejects_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, binaries = root / "sample", root / "bin"
            project.mkdir(); binaries.mkdir()
            curl = binaries / "curl"
            curl.write_text('''#!/usr/bin/env python3
import json,sys
req=json.loads(sys.argv[sys.argv.index('--data')+1])
if req['params']['name']=='list_projects':
 data={'projects':[{'id':'p','name':'sample'}]}
else:
 availability=req['params']['arguments']['filter']['availableFor']
 status={'work':'todo','review':'in_review','acceptance':'wait_accept'}[availability]
 data={'summary':{'byStatus':{status:1}},'tasks':[{'id':'t','status':status}]}
print(json.dumps({'result':{'structuredContent':data}}))
''')
            provider = binaries / "claude"
            provider.write_text('''#!/usr/bin/env python3
import os,time
from pathlib import Path
name=os.environ['WACHA_AGENT_NAME']
Path(os.environ['RALPH_TEST_DIR'], name+'.started').write_text(name)
time.sleep(120)
''')
            curl.chmod(0o755); provider.chmod(0o755)
            env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "RALPH_TEST_DIR": directory}
            launcher, _ = install_global(root / "share", root / "global-bin")
            subprocess.run([str(launcher), "init"], cwd=project, env=env, check=True, capture_output=True)
            config_path = project / ".ralph/config.json"
            config = json.loads(config_path.read_text())
            config["github"]["enabled"] = True
            config_path.write_text(json.dumps(config))
            (project / ".ralph/mcp.json").write_text('{"mcpServers": {}}')
            helper = root / "share/agent-foundation/ralph/runtime/lib/github_workflow.py"
            helper.write_text("import json,os,sys\nfrom pathlib import Path\n"
                              "if sys.argv[1]=='prepare': print(json.dumps({'cwd':str(Path(os.environ['RALPH_TEST_DIR'])/'sample'),'pending':False}))\n")
            processes = []
            try:
                for role, principal in [("worker", "alice"), ("worker", "bob"), ("reviewer", "alice")]:
                    process = subprocess.Popen([str(launcher), "run", role, "-p", principal], cwd=project,
                                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append(process)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not all((root / (p+".started")).exists() for p in ("alice", "bob")):
                    time.sleep(.05)
                self.assertTrue((root / "alice.started").exists())
                self.assertTrue((root / "bob.started").exists())
                self.assertTrue(all(p.poll() is None for p in processes))
                duplicate = subprocess.run([str(launcher), "run", "worker", "--principal", "alice"],
                                           cwd=project, env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(2, duplicate.returncode)
                self.assertIn("principal=alice role=worker", duplicate.stderr)
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    process.communicate(timeout=8)
            # The OS releases locks after exit; stale lock filenames do not block restart.
            handles = acquire_roles(project, {"worker": "alice"})
            for descriptor in handles:
                os.close(descriptor)
