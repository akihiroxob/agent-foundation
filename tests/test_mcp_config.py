from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ralph.lib.github_workflow import Workflow, WorkflowError
from ralph.lib.mcp_config import provider_arguments, read_servers


ROOT = Path(__file__).resolve().parents[1]


class McpConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project with spaces"
        self.root.mkdir()
        self.path = self.root / ".mcp.json"
        self.servers = {
            "wacha": {"type": "http", "url": "http://ignored/mcp"},
            "custom-tool": {"command": "node", "args": ["server with spaces.js", 'a"b', "$(do-not-run)"],
                            "env": {"TOKEN": "${TOKEN}", "TEXT": 'line1\nline2"'}, "cwd": str(self.root)},
            "remote": {"type": "http", "url": "https://example.test/mcp", "timeout": 90000,
                       "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}", "X-Key": "${OTHER_TOKEN}",
                                   "X-Region": 'east"west'}},
        }
        self.path.write_text(json.dumps({"mcpServers": self.servers}))

    def parsed_codex(self):
        args = provider_arguments("codex", self.path)
        self.assertEqual(args[::2], ["-c"] * (len(args) // 2))
        servers = {}
        for assignment in args[1::2]:
            key, _, value = assignment.partition("=")
            prefix, name, field = key.split(".", 2)
            self.assertEqual(prefix, "mcp_servers")
            servers.setdefault(name, {})[field] = value if value.startswith("{") else json.loads(value)
        return servers

    def test_arbitrary_stdio_http_and_authentication_references(self):
        servers = self.parsed_codex()
        self.assertNotIn("wacha", servers)
        stdio = servers["custom-tool"]
        self.assertEqual(stdio["args"], self.servers["custom-tool"]["args"])
        self.assertEqual(stdio["cwd"], str(self.root))
        self.assertEqual(stdio["env_vars"], ["TOKEN"])
        self.assertEqual(stdio["env"], '{"TEXT"=' + json.dumps('line1\nline2"') + '}')
        http = servers["remote"]
        self.assertEqual(http["bearer_token_env_var"], "REMOTE_TOKEN")
        self.assertEqual(http["env_http_headers"], '{"X-Key"="OTHER_TOKEN"}')
        self.assertEqual(http["http_headers"], '{"X-Region"=' + json.dumps('east"west') + '}')
        self.assertEqual(http["tool_timeout_sec"], 90)

    def test_claude_receives_all_configured_servers(self):
        args = provider_arguments("claude", self.path)
        self.assertEqual(args[:3], ["--mcp-config", str(self.path), "--strict-mcp-config"])
        self.assertEqual(args[4:], ["mcp__wacha", "mcp__custom-tool", "mcp__remote"])
        self.assertEqual(read_servers(self.path), self.servers)

    def test_missing_optional_project_config_and_invalid_definitions(self):
        self.path.unlink()
        self.assertEqual(provider_arguments("codex", self.path), [])
        self.assertEqual(provider_arguments("claude", self.path), [])
        for value in ([], {}, {"mcpServers": []}, {"mcpServers": {"bad.name": {"command": "node"}}},
                      {"mcpServers": {"bad": {"command": "node", "args": "--oops"}}},
                      {"mcpServers": {"bad": {"type": "http", "url": "x", "headers": {"X": 2}}}}):
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                read_servers(self.path)

    def test_untranslatable_fields_fail_explicitly_without_credentials(self):
        for server in ({"type": "sse", "url": "https://example.test/sse"},
                       {"command": "node", "env": {"ALIAS": "${TOKEN}"}},
                       {"type": "http", "url": "https://example.test/mcp", "headers": {"X": "prefix ${TOKEN}"}}):
            self.path.write_text(json.dumps({"mcpServers": {"custom": server}}))
            result = subprocess.run(["python3", str(ROOT / "ralph/lib/mcp_config.py"), "--provider", "codex",
                                     "--config", str(self.path)], capture_output=True,
                                    env={**os.environ, "TOKEN": "secret-not-in-output"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"secret-not-in-output", result.stderr)

    def test_project_config_is_preserved_and_runtime_connections_override_reserved_names(self):
        config = self.root / ".ralph/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"roles": {"worker": {"agentName": "worker"}},
                                     "wacha": {"url": "http://actual/mcp"},
                                     "verification": {"hostCommands": [["npm", "test"]]}}))
        original = self.path.read_bytes()
        servers = Workflow(config, "worker").agent_mcp_config("active-claim")["mcpServers"]
        self.assertEqual(servers["custom-tool"], self.servers["custom-tool"])
        self.assertEqual(servers["remote"], self.servers["remote"])
        self.assertEqual(servers["wacha"]["url"], "http://actual/mcp")
        self.assertEqual(servers["ralph_verification"]["args"][-2:], ["--claim-id", "active-claim"])
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("invalid json")
        with self.assertRaisesRegex(WorkflowError, "Cannot read MCP configuration"):
            Workflow(config, "worker").agent_mcp_config("active-claim")

    def test_provider_invocations_use_generic_adapter_in_both_execution_modes(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("inspect")
        capture = self.root / "args.json"
        fake = self.root / "fake-agent"
        fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n')
        fake.chmod(0o755)
        config = self.root / ".ralph/config.json"
        config.parent.mkdir()
        config.write_text("{}")
        runtime_path = config.parent / "mcp.json"
        runtime_path.write_bytes(self.path.read_bytes())
        for provider in ("codex", "claude"):
            for github in ("true", "false"):
                env = {**os.environ, "PROVIDER": str(ROOT / f"ralph/providers/{provider}.sh"),
                       "PROMPT": str(prompt), "PROJECT": str(self.root), "CAPTURE": str(capture),
                       "RALPH_AGENT_COMMAND": str(fake), "RALPH_CONFIG_PATH": str(config),
                       "WACHA_MCP_URL": "http://actual/mcp", "GITHUB_ENABLED": github}
                result = subprocess.run(["bash", "-c", 'set -u; source "$PROVIDER"; provider_run "$PROMPT" "$PROJECT"'],
                                        capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(capture.read_text())
                if provider == "codex":
                    self.assertIn('mcp_servers.custom-tool.command="node"', args)
                    self.assertIn('mcp_servers.wacha.url="http://actual/mcp"', args)
                    self.assertIn('mcp_servers.remote.bearer_token_env_var="REMOTE_TOKEN"', args)
                else:
                    self.assertIn("mcp__custom-tool", args)
                    self.assertIn("mcp__remote", args)
                    self.assertIn(str(runtime_path if github == "true" else self.path), args)
                capture.unlink()
                bad_path = runtime_path if github == "true" else self.path
                original = bad_path.read_bytes()
                bad_path.write_text("invalid json")
                failed = subprocess.run(["bash", "-c", 'set -u; source "$PROVIDER"; provider_run "$PROMPT" "$PROJECT"'],
                                        capture_output=True, text=True, env=env)
                self.assertNotEqual(failed.returncode, 0)
                self.assertFalse(capture.exists())
                bad_path.write_bytes(original)
        self.path.unlink()
        for provider in ("codex", "claude"):
            env.update(PROVIDER=str(ROOT / f"ralph/providers/{provider}.sh"), GITHUB_ENABLED="false")
            result = subprocess.run(["bash", "-c", 'set -u; source "$PROVIDER"; provider_run "$PROMPT" "$PROJECT"'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(capture.exists())
            capture.unlink()


if __name__ == "__main__":
    unittest.main()
