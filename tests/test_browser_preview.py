from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ralph.lib.browser_preview import PreviewSession, browser_config, playwright_config
from ralph.lib.github_workflow import Workflow, WorkflowError, atomic_json, run
from ralph.lib.host_verification import VerificationServer


class BrowserPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project with spaces"
        self.root.mkdir()
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=self.root)
        run("git", "config", "user.email", "test@example.test", cwd=self.root)
        (self.root / "app.txt").write_text("initial")
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "Initial", cwd=self.root)
        self.cwd = self.root / ".ralph/worktrees/manager/manager"
        run("git", "worktree", "add", "--detach", str(self.cwd), "HEAD", cwd=self.root)
        self.config = self.root / ".ralph/config.json"
        self.command = [sys.executable, "-u", "-c", "import http.server,os; print(os.getcwd()); print(os.environ.get('CUSTOM_SECRET_TOKEN','absent')); print(os.environ['FIXTURE_PATH']); http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),http.server.SimpleHTTPRequestHandler).serve_forever()"]
        self.settings = {"roles": {r: {"agentName": r} for r in ("worker", "reviewer", "manager")},
            "wacha": {"url": "http://unused/mcp"}, "verification": {"browser": {"command": self.command,
            "env": {"PORT": "{port}", "FIXTURE_PATH": "{dataDir}/fixture.db"}}}}
        atomic_json(self.config, self.settings)
        self.state = {"claimId": "claim123", "task": {"id": "task"}, "cwd": str(self.cwd)}
        self.state_path = self.root / ".ralph/github-state/manager/manager.json"
        atomic_json(self.state_path, self.state)
        self.playwright = {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@0.0.83", "--isolated", "--headless"], "env": {"EXAMPLE": 'value with "quotes"'}}
        atomic_json(self.root / ".mcp.json", {"mcpServers": {"playwright": self.playwright, "unrelated": {"command": "never"}}})
        self.renewal = patch.object(Workflow, "renew")
        self.renewal.start()
        self.addCleanup(self.renewal.stop)
        self.server = VerificationServer(self.config, "manager", "claim123")
        self.addCleanup(self.server.preview.stop)

    def require_loopback(self):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 0))
            except PermissionError:
                self.skipTest("Loopback binding requires host execution")

    def test_manager_only_deployed_definition_and_missing_install(self):
        config = Workflow(self.config, "manager").agent_mcp_config("claim123")["mcpServers"]
        self.assertEqual(config["playwright"]["args"][:-2], self.playwright["args"])
        self.assertEqual(config["playwright"]["args"][-2], "--output-dir")
        self.assertIn("ralph_verification", config)
        self.assertEqual(config["unrelated"], {"command": "never"})
        for role in ("worker", "reviewer"):
            role_servers = Workflow(self.config, role).agent_mcp_config("claim123")["mcpServers"]
            self.assertEqual(role_servers["playwright"], self.playwright)
            self.assertNotIn("ralph_verification", role_servers)
        (self.root / ".mcp.json").unlink()
        with self.assertRaisesRegex(WorkflowError, "Install the APM"):
            Workflow(self.config, "manager").agent_mcp_config("claim123")

    def test_invalid_configuration_and_shared_profiles_fail_closed(self):
        for value in (False, {}, {"command": "npm start"}, {"command": ["npm"], "cwd": "/tmp"},
                      {"command": ["npm"], "env": {"GITHUB_TOKEN": "secret"}},
                      {"command": ["npm"], "readyPath": "https://external.test"}):
            with self.assertRaises(WorkflowError):
                browser_config({"verification": {"browser": value}})
        for args in (["--headless"], ["--isolated", "--headless", "--cdp-endpoint=http://shared"]):
            atomic_json(self.root / ".mcp.json", {"mcpServers": {"playwright": {**self.playwright, "args": args}}})
            with self.assertRaises(WorkflowError):
                playwright_config(self.root)

    def test_tools_do_not_accept_agent_command_or_inactive_claim(self):
        tools = self.server.handle({"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"start_browser_preview", "stop_browser_preview"})
        result = self.server.handle({"id": 2, "method": "tools/call", "params": {"name": "start_browser_preview", "arguments": {"command": ["unapproved"]}}})["result"]
        self.assertTrue(result["isError"])
        self.state["claimId"] = "changed"
        atomic_json(self.state_path, self.state)
        with self.assertRaisesRegex(WorkflowError, "active execution"):
            self.server.preview.start({})

    def test_real_preview_isolated_data_environment_and_shutdown_evidence(self):
        self.require_loopback()
        with patch.dict(os.environ, {"CUSTOM_SECRET_TOKEN": "sensitive", "SSH_AUTH_SOCK": "sensitive"}):
            record = self.server.preview.start({})
        self.assertEqual(record["status"], "ready")
        self.assertEqual(self.server.preview.start({}), record)
        process = self.server.preview.process
        self.assertNotEqual(Path(record["dataDirectory"]), self.cwd)
        log = Path(record["logPath"]).read_text()
        self.assertIn(str(self.cwd), log)
        self.assertIn(record["dataDirectory"], log)
        self.assertIn("absent", log)
        self.assertNotIn("sensitive", log)
        stopped = self.server.preview.stop()
        self.assertTrue(stopped["sourceUnchanged"])
        self.assertIsNotNone(process.poll())
        self.assertEqual(json.loads(Path(record["recordPath"]).read_text()), stopped)

    def test_failure_timeout_and_changed_source_records(self):
        self.require_loopback()
        self.settings["verification"]["browser"]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
        atomic_json(self.config, self.settings)
        with self.assertRaisesRegex(WorkflowError, "exited"):
            self.server.preview.start({})
        self.assertEqual(self.server.preview.record["status"], "startup_failed")
        self.settings["verification"]["browser"]["command"] = [sys.executable, "-c", "import time;time.sleep(30)"]
        atomic_json(self.config, self.settings)
        with patch.object(PreviewSession, "START_TIMEOUT", 0.1):
            with self.assertRaisesRegex(WorkflowError, "timed out"):
                self.server.preview.start({})
        self.settings["verification"]["browser"]["command"] = self.command
        atomic_json(self.config, self.settings)
        self.server.preview.start({})
        (self.cwd / "app.txt").write_text("changed")
        self.assertFalse(self.server.preview.stop()["sourceUnchanged"])

    def test_expired_claim_stops_running_server(self):
        self.require_loopback()
        with patch.object(PreviewSession, "RENEW_INTERVAL", 0.05):
            self.server.preview.start({})
            process = self.server.preview.process
            self.state["claimId"] = "changed"
            atomic_json(self.state_path, self.state)
            deadline = time.monotonic() + 3
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertIsNotNone(process.poll())
        # Process exit precedes the monitor's record update; wait for stop() to finish.
        with self.server.preview.lock:
            self.assertEqual(self.server.preview.record["status"], "claim_invalid")
            self.assertEqual(json.loads(self.server.preview.record_path.read_text())["status"], "claim_invalid")

    def test_session_timeout_stops_running_server(self):
        self.require_loopback()
        with patch.object(PreviewSession, "RENEW_INTERVAL", 0.01), patch.object(PreviewSession, "SESSION_TIMEOUT", 0.05):
            self.server.preview.start({})
            process = self.server.preview.process
            deadline = time.monotonic() + 3
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertIsNotNone(process.poll())
        with self.server.preview.lock:
            self.assertEqual(self.server.preview.record["status"], "timeout")
            self.assertEqual(json.loads(self.server.preview.record_path.read_text())["status"], "timeout")

    def test_mcp_disconnect_stops_preview_without_agent_cleanup(self):
        self.require_loopback()
        root = Path(__file__).resolve().parents[1]
        code = 'from ralph.lib.github_workflow import Workflow; Workflow.renew=lambda self: None; from ralph.lib.host_verification import main; main()'
        process = subprocess.Popen([sys.executable, "-u", "-c", code, "--config", str(self.config),
            "--role", "manager", "--claim-id", "claim123"], cwd=root, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            process.stdin.write(json.dumps({"id": 1, "method": "tools/call", "params": {
                "name": "start_browser_preview", "arguments": {}}}) + "\n")
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            self.assertFalse(response["result"]["isError"])
            record = response["result"]["structuredContent"]
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
            stopped = json.loads(Path(record["recordPath"]).read_text())
            self.assertEqual(stopped["status"], "client_disconnected")
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", int(record["url"].rsplit(":", 1)[1]))), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_children_cannot_change_mcp_stdin_or_disconnect_preview(self):
        self.require_loopback()
        # Model Node's stdin handling without requiring Node in the test environment.
        change_stdin = "import fcntl,os; fcntl.fcntl(0,fcntl.F_SETFL,fcntl.fcntl(0,fcntl.F_GETFL)|os.O_NONBLOCK); "
        self.settings["verification"]["browser"]["command"] = [sys.executable, "-u", "-c",
            change_stdin + "import http.server; http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),http.server.SimpleHTTPRequestHandler).serve_forever()"]
        command = [sys.executable, "-c", change_stdin + "print('host verification complete')"]
        self.settings["verification"]["hostCommands"] = [command]
        atomic_json(self.config, self.settings)
        root = Path(__file__).resolve().parents[1]
        code = 'from ralph.lib.github_workflow import Workflow; Workflow.renew=lambda self: None; from ralph.lib.host_verification import main; main()'
        with (self.root / "mcp-stderr.log").open("w") as error_log:
            process = subprocess.Popen([sys.executable, "-u", "-c", code, "--config", str(self.config),
                "--role", "manager", "--claim-id", "claim123"], cwd=root, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=error_log, text=True)
            def request(identifier, method, params=None):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method,
                    "params": params or {}}) + "\n")
                process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 10)[0], "MCP response timed out")
                line = process.stdout.readline()
                self.assertTrue(line, "MCP disconnected while its client kept stdin open")
                return json.loads(line)["result"]
            try:
                request(1, "initialize", {"protocolVersion": "2025-03-26"})
                request(2, "tools/list")
                preview = request(3, "tools/call", {"name": "start_browser_preview", "arguments": {}})
                self.assertFalse(preview["isError"])
                url = preview["structuredContent"]["url"]
                self.assertEqual(request(4, "ping"), {})
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                host = request(5, "tools/call", {"name": "run_host_verification", "arguments": {"command": command}})
                self.assertFalse(host["isError"])
                self.assertEqual(host["structuredContent"]["exitCode"], 0)
                self.assertEqual(request(6, "ping"), {})
                self.assertIsNone(process.poll())
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                request(7, "tools/call", {"name": "stop_browser_preview", "arguments": {}})
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
                process.stdout.close()

    def test_preview_preparation_creates_isolated_fixture_and_records_evidence(self):
        self.require_loopback()
        command = [sys.executable, "-c", "from pathlib import Path; import os; p=Path(os.environ['FIXTURE_PATH']); p.write_text('fixture'); print('scenario=display, id=example, role=viewer')"]
        self.settings["verification"]["browser"]["setupCommands"] = [command]
        atomic_json(self.config, self.settings)
        record = self.server.preview.start({})
        self.assertEqual(record["status"], "ready")
        self.assertEqual((Path(record["dataDirectory"]) / "fixture.db").read_text(), "fixture")
        self.assertEqual(record["preparation"][0]["exitCode"], 0)
        self.assertIn("role=viewer", Path(record["preparation"][0]["logPath"]).read_text())
        self.assertEqual(self.server.preview.stop()["sourceUnchanged"], True)

    def test_preparation_failure_timeout_and_source_changes_prevent_preview(self):
        self.require_loopback()
        from ralph.lib.browser_preview import PreviewSession
        for code, message, timeout in [("raise SystemExit(3)", "preparation failed", 120),
                ("import time; time.sleep(5)", "preparation timed out", 0.05),
                ("from pathlib import Path; Path('app.txt').write_text('changed')", "changed source", 120)]:
            self.settings["verification"]["browser"]["setupCommands"] = [[sys.executable, "-c", code]]
            atomic_json(self.config, self.settings)
            with patch.object(PreviewSession, "START_TIMEOUT", timeout):
                with self.assertRaisesRegex(WorkflowError, message):
                    self.server.preview.start({})
            self.assertIsNone(self.server.preview.process)
            self.assertEqual(self.server.preview.record["status"], "startup_failed")
            self.assertNotIn("Compass running", Path(self.server.preview.record["logPath"]).read_text())
            (self.cwd / "app.txt").write_text("initial")

    def test_preparation_configuration_rejects_shell_strings_and_invalid_arguments(self):
        for commands in ("npm run seed", ["npm"], [[]], [[""]], [[1]]):
            with self.assertRaisesRegex(WorkflowError, "setupCommands"):
                browser_config({"verification": {"browser": {"command": ["npm", "start"], "setupCommands": commands}}})

    def test_both_providers_receive_playwright_definition(self):
        root = Path(__file__).resolve().parents[1]
        atomic_json(self.cwd / ".ralph/mcp.json", Workflow(self.config, "manager").agent_mcp_config("claim123"))
        prompt = self.root / "prompt.txt"
        prompt.write_text("verify browser")
        capture = self.root / "arguments.txt"
        fake = self.root / "fake-agent"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\ncat >/dev/null\n')
        fake.chmod(0o755)
        for provider in ("codex", "claude"):
            subprocess.run(["bash", "-c", 'source "$PROVIDER"; provider_run "$PROMPT" "$WORKTREE"'], check=True,
                env={**os.environ, "PROVIDER": str(root / f"ralph/providers/{provider}.sh"), "PROMPT": str(prompt),
                    "WORKTREE": str(self.cwd), "RALPH_CONFIG_PATH": str(self.config), "RALPH_AGENT_COMMAND": str(fake),
                    "CAPTURE": str(capture), "GITHUB_ENABLED": "true", "WACHA_MCP_URL": "http://unused/mcp"})
            args = capture.read_text()
            if provider == "codex":
                self.assertIn("mcp_servers.playwright.command=", args)
                self.assertIn("--isolated", args)
                self.assertIn('mcp_servers.playwright.env={"EXAMPLE"="value with \\"quotes\\""', args)
                self.assertIn("mcp_servers.playwright.required=true", args)
            else:
                self.assertIn("--strict-mcp-config", args)
                self.assertIn("mcp__playwright", args)


if __name__ == "__main__":
    unittest.main()
