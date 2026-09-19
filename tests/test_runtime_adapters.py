import json
import unittest
from pathlib import Path

from adapters.runtime_adapters import render_claude, render_codex

ROOT = Path(__file__).resolve().parents[1]


class RuntimeAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = json.loads((ROOT / "policy/runtime-policy.json").read_text(encoding="utf-8"))

    def test_codex_contains_expected_decisions(self):
        output = render_codex(self.policy)
        rules = output[".codex/rules/default.rules"]
        self.assertIn('decision = "allow"', rules)
        self.assertIn('decision = "prompt"', rules)
        self.assertIn('decision = "forbidden"', rules)
        self.assertIn('sandbox_mode = "workspace-write"', output[".codex/config.toml"])

    def test_codex_allows_configured_wacha_tools(self):
        config = render_codex(self.policy)[".codex/config.toml"]
        for tool in self.policy["mcp"]["wacha"]["allow"]:
            self.assertIn(f"[mcp_servers.wacha.tools.{tool}]", config)
        self.assertEqual(len(self.policy["mcp"]["wacha"]["allow"]), config.count('approval_mode = "approve"'))

    def test_claude_contains_permission_groups(self):
        output = render_claude(self.policy)
        settings = json.loads(output[".claude/settings.json"])
        permissions = settings["permissions"]
        self.assertEqual("acceptEdits", permissions["defaultMode"])
        self.assertTrue(permissions["allow"])
        self.assertTrue(permissions["ask"])
        self.assertTrue(permissions["deny"])
        self.assertIn("Read(./.env)", permissions["deny"])
        self.assertIn("Bash(pkill *)", permissions["deny"])
        self.assertIn("Bash(killall *)", permissions["deny"])

    def test_claude_allows_configured_mcp_servers(self):
        settings = json.loads(render_claude(self.policy)[".claude/settings.json"])
        for server in self.policy["mcp"]:
            self.assertIn(f"mcp__{server}", settings["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()
