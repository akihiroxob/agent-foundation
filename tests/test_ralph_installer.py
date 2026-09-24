import json
import os
import re
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
            self.assertTrue((target / ".ralph/runtime/providers/codex.sh").is_file())
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
            ROOT / "ralph/providers/codex.sh",
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
printf '%s\\n' "$@" >"$RALPH_TEST_STATE_DIR/claude-args"
if (( count == 1 )); then
  printf '%s\\n' "You've hit your limit · resets later" >&2
  exit 42
fi
if (( count == 2 )); then
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
            config["retry"] = {"initialSeconds": 1, "tokenLimitSeconds": 2}
            config["roles"]["worker"]["model"] = "test-claude-model"
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
            self.assertTrue((Path(directory) / "done").exists(), f"stdout={stdout!r} stderr={stderr!r}")
            self.assertEqual("3", (Path(directory) / "count").read_text(encoding="utf-8").strip())
            self.assertIn("Worker対象: todo=1", stdout)
            self.assertIn("Token上限に達しました", stderr)
            self.assertIn("2秒後に再試行", stderr)
            self.assertIn("終了コード 42", stderr)
            self.assertIn("1秒後に再試行", stderr)
            timestamp_pattern = re.compile(
                r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[worker\] "
            )
            self.assertTrue(all(timestamp_pattern.match(line) for line in stdout.splitlines()))
            self.assertTrue(all(timestamp_pattern.match(line) for line in stderr.splitlines()))
            log_path = target / ".ralph/logs/ralph.log"
            self.assertTrue(log_path.is_file())
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("[worker] Worker対象: todo=1", log)
            self.assertIn("[worker] You've hit your limit", log)
            claude_args = (Path(directory) / "claude-args").read_text(encoding="utf-8").splitlines()
            model_index = claude_args.index("--model")
            self.assertEqual("test-claude-model", claude_args[model_index + 1])
            requests = (Path(directory) / "requests").read_text(encoding="utf-8")
            self.assertIn('"availableFor":"work"', requests)

    def test_codex_provider_runs_with_wacha_mcp_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            bin_dir = Path(directory) / "bin"
            target.mkdir()
            bin_dir.mkdir()
            launcher, _ = install_global(Path(directory) / "share", Path(directory) / "global-bin")

            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
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

            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' "$@" >"$RALPH_TEST_STATE_DIR/codex-args"
printf '%s\\n' "$WACHA_AGENT_NAME" >"$RALPH_TEST_STATE_DIR/agent-name"
pwd >"$RALPH_TEST_STATE_DIR/codex-cwd"
cat >"$RALPH_TEST_STATE_DIR/codex-prompt"
touch "$RALPH_TEST_STATE_DIR/done"
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["RALPH_TEST_STATE_DIR"] = directory
            init_result = subprocess.run(
                [str(launcher), "init", "--agent-provider", "codex"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            config_path = target / ".ralph/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["agentProvider"] = "claude"
            config["roles"]["worker"]["agentProvider"] = "codex"
            config["roles"]["worker"]["command"] = str(fake_codex)
            config["roles"]["worker"]["model"] = "test-codex-model"
            config["codex"]["dangerouslyBypassApprovalsAndSandbox"] = True
            config["pollIntervalSeconds"] = 1
            config["retry"] = {"initialSeconds": 1, "tokenLimitSeconds": 2}
            config_path.write_text(json.dumps(config), encoding="utf-8")

            process = subprocess.Popen(
                [str(launcher), "run", "worker"],
                cwd=target,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not (Path(directory) / "done").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

            self.assertEqual(0, init_result.returncode)
            self.assertEqual("claude", config["agentProvider"])
            self.assertEqual("codex", config["roles"]["worker"]["agentProvider"])
            self.assertFalse((target / ".mcp.json").exists())
            self.assertFalse((target / ".claude/settings.json").exists())
            self.assertTrue((Path(directory) / "done").exists(), f"stdout={stdout!r} stderr={stderr!r}")
            codex_args = (Path(directory) / "codex-args").read_text(encoding="utf-8").splitlines()
            self.assertEqual("exec", codex_args[0])
            self.assertIn("--ephemeral", codex_args)
            model_index = codex_args.index("--model")
            self.assertEqual("test-codex-model", codex_args[model_index + 1])
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex_args)
            self.assertIn('mcp_servers.wacha.url="http://localhost:51743/mcp"', codex_args)
            self.assertIn(
                'mcp_servers.wacha.bearer_token_env_var="WACHA_AGENT_NAME"', codex_args
            )
            self.assertIn(
                'mcp_servers.wacha.default_tools_approval_mode="approve"', codex_args
            )
            self.assertIn("mcp_servers.wacha.required=true", codex_args)
            self.assertEqual("-", codex_args[-1])
            self.assertEqual(
                "worker-node-001",
                (Path(directory) / "agent-name").read_text(encoding="utf-8").strip(),
            )
            self.assertEqual(
                target.resolve(),
                Path((Path(directory) / "codex-cwd").read_text(encoding="utf-8").strip()),
            )
            prompt = (Path(directory) / "codex-prompt").read_text(encoding="utf-8")
            self.assertIn(str(target.resolve()), prompt)
            self.assertIn("sample-project", prompt)

    def test_idle_logging_suppresses_unchanged_poll_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            bin_dir = Path(directory) / "bin"
            target.mkdir()
            bin_dir.mkdir()
            launcher, _ = install_global(Path(directory) / "share", Path(directory) / "global-bin")

            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' request >>"$RALPH_TEST_STATE_DIR/idle-requests"
if [[ "$*" == *'list_projects'* ]]; then
  printf '%s\\n' '{"result":{"structuredContent":{"projects":[{"id":"project-1","name":"sample-project"}]}}}'
else
  printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"todo":0,"rejected":0,"doing":0,"in_review":0}},"tasks":[]}}}'
fi
""",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_claude = bin_dir / "claude"
            fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_claude.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["RALPH_TEST_STATE_DIR"] = directory
            subprocess.run(
                [str(launcher), "init"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            config_path = target / ".ralph/config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["pollIntervalSeconds"] = 1
            config["logging"]["idleHeartbeatSeconds"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")

            process = subprocess.Popen(
                [str(launcher), "run", "worker"],
                cwd=target,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            log_path = target / ".ralph/logs/ralph.log"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if log_path.exists() and "待機中です。" in log_path.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

            self.assertEqual("", stderr)
            log = log_path.read_text(encoding="utf-8")
            self.assertEqual(1, log.count("待機を開始します。"), f"stdout={stdout!r}")
            self.assertEqual(1, log.count("待機中です。"), f"stdout={stdout!r}")
            self.assertNotIn("秒待機します。", log)
            request_count = len(
                (Path(directory) / "idle-requests").read_text(encoding="utf-8").splitlines()
            )
            self.assertGreaterEqual(request_count, 4)

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
