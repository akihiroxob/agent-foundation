from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph.lib.github_workflow import Workflow, WorkflowError, atomic_json, run
from ralph.lib.host_verification import VerificationServer, commands


class HostVerificationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve() / "project with spaces"
        self.root.mkdir()
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=self.root)
        run("git", "config", "user.email", "test@example.test", cwd=self.root)
        (self.root / "app.txt").write_text("initial\n")
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "Initial", cwd=self.root)
        self.cwd = self.root / ".ralph/worktrees/worker-task-claim123"
        run("git", "worktree", "add", "--detach", str(self.cwd), "HEAD", cwd=self.root)
        self.config = self.root / ".ralph/config.json"
        self.command = [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ.get('CUSTOM_SECRET_TOKEN','absent'))"]
        atomic_json(self.config, {"roles": {"worker": {"agentName": "worker"}}, "wacha": {"url": "http://unused/mcp"},
            "verification": {"hostCommands": [self.command]}})
        self.state = {"claimId": "claim123", "task": {"id": "task"}, "cwd": str(self.cwd)}
        self.state_path = self.root / ".ralph/github-state/worker.json"
        atomic_json(self.state_path, self.state)
        self.server = VerificationServer(self.config, "worker", "claim123")
        self.renewal = patch.object(Workflow, "renew")
        self.renewal.start()
        self.addCleanup(self.renewal.stop)
        self.addCleanup(self.directory.cleanup)

    def approve(self, command):
        config = json.loads(self.config.read_text())
        config["verification"]["hostCommands"] = [command]
        atomic_json(self.config, config)

    def test_exact_matching_cwd_environment_and_persistent_record(self):
        with patch.dict(os.environ, {"CUSTOM_SECRET_TOKEN": "synthetic-secret", "WACHA_AGENT_NAME": "private-principal", "SSH_AUTH_SOCK": "private-socket"}):
            record = self.server.execute({"command": self.command})
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["exitCode"], 0)
        self.assertTrue(record["sourceUnchanged"])
        log = Path(record["logPath"]).read_text()
        self.assertIn(str(self.cwd), log)
        self.assertIn("absent", log)
        self.assertNotIn("synthetic-secret", log)
        self.assertEqual(json.loads(Path(record["logPath"]).with_suffix(".json").read_text()), record)
        for arguments in ({"command": self.command + ["extra"]}, {"command": ["npm"]},
                          {"command": self.command, "cwd": str(self.root)}):
            with self.assertRaisesRegex(WorkflowError, "exactly match"):
                self.server.execute(arguments)

    def test_invalid_config_and_expired_execution_fail_before_launch(self):
        for config in ({"verification": None}, {"verification": {"hostCommands": "npm test"}},
                       {"verification": {"hostCommands": [["npm", "test\0"]]}}):
            with self.assertRaises(WorkflowError):
                commands(config)
        self.state["claimId"] = "another-claim"
        atomic_json(self.state_path, self.state)
        with self.assertRaisesRegex(WorkflowError, "active execution"):
            self.server.execute({"command": self.command})
        self.state["claimId"] = "claim123"
        self.state["cwd"] = str(self.root)
        atomic_json(self.state_path, self.state)
        with self.assertRaisesRegex(WorkflowError, "registered Task"):
            self.server.execute({"command": self.command})

    def test_failure_timeout_and_changed_sources_are_not_success_evidence(self):
        command = [sys.executable, "-c", "print('failure'); raise SystemExit(7)"]
        self.approve(command)
        record = self.server.execute({"command": command})
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exitCode"], 7)
        command = [sys.executable, "-c", "from pathlib import Path; Path('app.txt').write_text('changed')"]
        self.approve(command)
        self.assertFalse(self.server.execute({"command": command})["sourceUnchanged"])
        command = [sys.executable, "-c", "import time; time.sleep(5)"]
        self.approve(command)
        with patch("ralph.lib.host_verification.TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.server.execute({"command": command})
        records = [json.loads(p.read_text()) for p in (self.root / ".ralph/verification/claim123").glob("*.json")]
        self.assertIn("timeout", [r["status"] for r in records])

    def test_tools_protocol_and_generated_mcp_config(self):
        response = self.server.handle({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        tools = self.server.handle({"id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(tools[0]["inputSchema"]["properties"]["command"]["enum"], [self.command])
        result = self.server.handle({"id": 3, "method": "tools/call", "params": {
            "name": "run_host_verification", "arguments": {"command": self.command}}})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["cwd"], str(self.cwd))
        workflow = Workflow(self.config, "worker")
        config = workflow.agent_mcp_config("claim123")
        self.assertEqual(config["mcpServers"]["ralph_verification"]["args"][-2:], ["--claim-id", "claim123"])
        self.assertEqual(config["mcpServers"]["ralph_verification"]["timeout"], 3660000)
        self.assertIsNone(self.server.handle({"method": "notifications/initialized"}))

    def test_actual_loopback_server_command(self):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("Loopback binding needs host execution outside the outer sandbox")
        finally:
            probe.close()
        command = [sys.executable, "-c", "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print('listening'); s.close()"]
        self.approve(command)
        record = self.server.execute({"command": command})
        self.assertEqual(record["exitCode"], 0)
        self.assertIn("listening", Path(record["logPath"]).read_text())

    def test_stdio_handshake_and_codex_provider_configuration(self):
        workflow = Workflow(self.config, "worker")
        atomic_json(self.cwd / ".ralph/mcp.json", workflow.agent_mcp_config("claim123"))
        helper = Path(__file__).resolve().parents[1] / "ralph/lib/host_verification.py"
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, str(helper), "--config", str(self.config), "--role", "worker", "--claim-id", "claim123"],
            input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2])
        prompt = self.root / "prompt.txt"
        prompt.write_text("Verify\n")
        fake = self.root / "fake-codex"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\ncat >/dev/null\n')
        fake.chmod(0o755)
        capture = self.root / "arguments.txt"
        provider = helper.parent.parent / "providers/codex.sh"
        subprocess.run(["bash", "-c", 'source "$PROVIDER"; provider_run "$PROMPT" "$WORKTREE"'], check=True,
            env={**os.environ, "PROVIDER": str(provider), "PROMPT": str(prompt), "WORKTREE": str(self.cwd),
                "RALPH_CONFIG_PATH": str(self.config), "RALPH_AGENT_COMMAND": str(fake), "CAPTURE": str(capture),
                "GITHUB_ENABLED": "true", "WACHA_MCP_URL": "http://unused/mcp"})
        arguments = capture.read_text()
        self.assertIn("mcp_servers.ralph_verification.command=", arguments)
        self.assertIn("mcp_servers.ralph_verification.args=", arguments)
        self.assertIn("--claim-id", arguments)
        self.assertIn("mcp_servers.ralph_verification.required=true", arguments)
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\nprintf "%s" "$CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS" > "$BACKGROUND_CAPTURE"\n')
        background_capture = self.root / "background.txt"
        subprocess.run(["bash", "-c", 'source "$PROVIDER"; provider_run "$PROMPT" "$WORKTREE"'], check=True,
            env={**os.environ, "PROVIDER": str(helper.parent.parent / "providers/claude.sh"),
                "PROMPT": str(prompt), "WORKTREE": str(self.cwd), "RALPH_CONFIG_PATH": str(self.config),
                "RALPH_AGENT_COMMAND": str(fake), "CAPTURE": str(capture), "GITHUB_ENABLED": "true",
                "BACKGROUND_CAPTURE": str(background_capture), "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "120000"})
        self.assertEqual(background_capture.read_text(), "0")
        self.assertIn("--strict-mcp-config", capture.read_text())


if __name__ == "__main__":
    unittest.main()
