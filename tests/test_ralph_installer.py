import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from scripts.install_ralph import install_global, install_local

ROOT = Path(__file__).resolve().parents[1]


class RalphInstallerTest(unittest.TestCase):
    def test_installs_local_runtime_and_project_config(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            target.mkdir()

            executable, created_config = install_local(target)

            self.assertTrue(created_config)
            self.assertTrue(executable.is_file())
            self.assertTrue(executable.stat().st_mode & 0o111)
            self.assertTrue((target / ".ralph/runtime/backends/wacha.sh").is_file())
            self.assertTrue((target / ".ralph/runtime/providers/claude.sh").is_file())
            self.assertTrue((target / ".ralph/runtime/prompts/worker.md").is_file())
            config = json.loads((target / ".ralph/config.json").read_text(encoding="utf-8"))
            self.assertEqual("sample-project", config["projectName"])
            mcp_config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual("http", mcp_config["mcpServers"]["wacha"]["type"])
            settings = json.loads((target / ".claude/settings.json").read_text(encoding="utf-8"))
            self.assertIn("mcp__wacha", settings["permissions"]["allow"])
            self.assertIn("wacha", settings["enabledMcpjsonServers"])

    def test_local_reinstall_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            config_path = target / ".ralph/config.json"
            config_path.parent.mkdir(parents=True)
            original = '{"projectName": "custom", "wacha": {"url": "http://wacha.test/mcp"}}\n'
            config_path.write_text(original, encoding="utf-8")

            _, created_config = install_local(target)

            self.assertFalse(created_config)
            self.assertEqual(original, config_path.read_text(encoding="utf-8"))

    def test_installs_global_runtime_and_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            launcher, runtime_dir = install_global(root / "share", root / "bin")

            self.assertEqual(root.resolve() / "bin/ralph", launcher)
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertTrue((runtime_dir / "bin/ralph").is_file())
            self.assertTrue((runtime_dir / "bin/ralph-loop").is_file())
            self.assertTrue((runtime_dir / "examples/config.json").is_file())
            self.assertNotIn(".ralph", {path.name for path in root.iterdir()})

    def test_shell_sources_have_valid_syntax(self):
        shell_files = [
            ROOT / "ralph/bin/ralph-loop",
            ROOT / "ralph/backends/wacha.sh",
            ROOT / "ralph/providers/claude.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, shell_files)], check=True)

    def test_global_cli_initializes_and_runs_project(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            bin_dir = Path(directory) / "bin"
            target.mkdir()
            bin_dir.mkdir()
            launcher, _ = install_global(Path(directory) / "share", Path(directory) / "global-bin")

            (target / ".claude").mkdir()
            (target / ".mcp.json").write_text(
                '{"mcpServers":{"other":{"type":"http","url":"http://other.test/mcp"}}}\n',
                encoding="utf-8",
            )
            (target / ".claude/settings.json").write_text(
                '{"permissions":{"allow":["Bash"],"deny":["Bash(git push)"]},"custom":true}\n',
                encoding="utf-8",
            )

            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$RALPH_TEST_STATE_DIR/requests"
if [[ "$*" == *'list_projects'* ]]; then
  printf '%s\\n' '{"result":{"structuredContent":{"projects":[{"id":"project-1","name":"sample-project"}]}}}'
elif [[ -f "$RALPH_TEST_STATE_DIR/done" ]]; then
  printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"todo":0,"rejected":0,"doing":0,"in_review":0,"accepted":1}},"tasks":[]}}}'
else
  printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"todo":1,"rejected":0,"doing":0,"in_review":0}},"tasks":[{"id":"task-1"}]}}}'
fi
""",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                """#!/usr/bin/env bash
count=0
if [[ -f "$RALPH_TEST_STATE_DIR/count" ]]; then
  count="$(<"$RALPH_TEST_STATE_DIR/count")"
fi
count=$((count + 1))
printf '%s\\n' "$count" >"$RALPH_TEST_STATE_DIR/count"
if (( count == 1 )); then
  exit 42
fi
touch "$RALPH_TEST_STATE_DIR/done"
exit 0
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["RALPH_TEST_STATE_DIR"] = directory
            init_result = subprocess.run(
                [str(launcher), "init"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            reinit_result = subprocess.run(
                [str(launcher), "init"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            config_path = target / ".ralph/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["pollIntervalSeconds"] = 1
            config["retry"] = {"initialSeconds": 1, "maxSeconds": 1}
            config_path.write_text(json.dumps(config), encoding="utf-8")

            process = subprocess.Popen(
                [str(launcher), "run", "worker"],
                cwd=target,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 8
            while not (Path(directory) / "done").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

            self.assertEqual(0, init_result.returncode)
            self.assertEqual(0, reinit_result.returncode)
            self.assertTrue((target / ".ralph/config.json").is_file())
            mcp_config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertIn("other", mcp_config["mcpServers"])
            self.assertEqual(
                "Bearer ${WACHA_AGENT_NAME}",
                mcp_config["mcpServers"]["wacha"]["headers"]["Authorization"],
            )
            settings = json.loads((target / ".claude/settings.json").read_text(encoding="utf-8"))
            self.assertTrue(settings["custom"])
            self.assertIn("Bash", settings["permissions"]["allow"])
            self.assertIn("mcp__wacha", settings["permissions"]["allow"])
            self.assertEqual(1, settings["permissions"]["allow"].count("mcp__wacha"))
            self.assertIn("Bash(git push)", settings["permissions"]["deny"])
            self.assertIn("wacha", settings["enabledMcpjsonServers"])
            self.assertEqual(1, settings["enabledMcpjsonServers"].count("wacha"))
            self.assertTrue((Path(directory) / "done").exists())
            self.assertEqual("2", (Path(directory) / "count").read_text(encoding="utf-8").strip())
            self.assertIn("Worker対象: todo=1", stdout)
            self.assertIn("終了コード 42", stderr)
            self.assertIn("1秒後に再試行", stderr)
            requests = (Path(directory) / "requests").read_text(encoding="utf-8")
            self.assertIn('"availableFor":"work"', requests)

    def test_init_rejects_invalid_claude_settings_without_overwriting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            target.mkdir()
            settings_path = target / ".claude/settings.json"
            settings_path.parent.mkdir()
            original = '{"permissions":{"allow":"invalid"}}\n'
            settings_path.write_text(original, encoding="utf-8")
            launcher, _ = install_global(Path(directory) / "share", Path(directory) / "global-bin")

            result = subprocess.run(
                [str(launcher), "init"],
                cwd=target,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("permissions.allow must be a string array", result.stdout)
            self.assertEqual(original, settings_path.read_text(encoding="utf-8"))
            self.assertFalse((target / ".mcp.json").exists())


if __name__ == "__main__":
    unittest.main()
