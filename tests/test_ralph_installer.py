import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.install_ralph import install

ROOT = Path(__file__).resolve().parents[1]


class RalphInstallerTest(unittest.TestCase):
    def test_installs_runtime_and_project_config(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            target.mkdir()

            executable, created_config = install(target)

            self.assertTrue(created_config)
            self.assertTrue(executable.is_file())
            self.assertTrue(executable.stat().st_mode & 0o111)
            self.assertTrue((target / ".ralph/runtime/backends/wacha.sh").is_file())
            self.assertTrue((target / ".ralph/runtime/providers/claude.sh").is_file())
            self.assertTrue((target / ".ralph/runtime/prompts/worker.md").is_file())
            config = json.loads((target / ".ralph/config.json").read_text(encoding="utf-8"))
            self.assertEqual("sample-project", config["projectName"])

    def test_keeps_existing_config_when_reinstalling(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            config_path = target / ".ralph/config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"projectName": "custom"}\n', encoding="utf-8")

            _, created_config = install(target)

            self.assertFalse(created_config)
            self.assertEqual('{"projectName": "custom"}\n', config_path.read_text(encoding="utf-8"))

    def test_shell_sources_have_valid_syntax(self):
        shell_files = [
            ROOT / "ralph/bin/ralph-loop",
            ROOT / "ralph/backends/wacha.sh",
            ROOT / "ralph/providers/claude.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, shell_files)], check=True)

    def test_installed_runner_stops_when_agent_does_not_change_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample-project"
            bin_dir = Path(directory) / "bin"
            target.mkdir()
            bin_dir.mkdir()
            executable, _ = install(target)

            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
if [[ "$*" == *'list_projects'* ]]; then
  printf '%s\\n' '{"result":{"structuredContent":{"projects":[{"id":"project-1","name":"sample-project"}]}}}'
else
  printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"todo":1,"rejected":0,"doing":0,"in_review":0}}}}}'
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
            result = subprocess.run(
                [str(executable), "worker"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("Worker対象: todo=1", result.stdout)
            self.assertIn("Task状態が変化しなかった", result.stderr)


if __name__ == "__main__":
    unittest.main()
