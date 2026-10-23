from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from scripts.install_ralph import install_global


class RalphRecoveryTest(unittest.TestCase):
    def test_no_progress_pause_survives_restart_and_can_be_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "sample"
            project.mkdir()
            binaries = root / "bin"
            binaries.mkdir()
            curl = binaries / "curl"
            curl.write_text('''#!/usr/bin/env bash
if [[ "$*" == *list_projects* ]]; then
  printf '%s\n' '{"result":{"structuredContent":{"projects":[{"id":"p","name":"sample"}]}}}'
else
  printf '%s\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"todo":1}},"tasks":[{"id":"t","status":"todo"}]}}}'
fi
''')
            provider = binaries / "claude"
            provider.write_text('''#!/usr/bin/env bash
printf 'run\n' >>"$RALPH_TEST_DIR/runs"
''')
            curl.chmod(0o755)
            provider.chmod(0o755)
            env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "RALPH_TEST_DIR": directory}
            launcher, _ = install_global(root / "share", root / "launchers")
            subprocess.run([str(launcher), "init"], cwd=project, env=env, check=True, capture_output=True)
            path = project / ".ralph/config.json"
            config = json.loads(path.read_text())
            config["pollIntervalSeconds"] = 1
            config["retry"]["initialSeconds"] = 1
            config["retry"]["maxNoProgressAttempts"] = 2
            path.write_text(json.dumps(config))
            pause = project / ".ralph/runtime.json"

            def start():
                return subprocess.Popen([str(launcher), "run", "worker"], cwd=project, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            def stop(process):
                process.terminate()
                return process.communicate(timeout=5)

            process = start()
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if pause.exists() and json.loads(pause.read_text()).get("agents", {}).get("worker-node-001", {}).get("worker", {}).get("paused"):
                        break
                    time.sleep(0.05)
            finally:
                _, stderr = stop(process)
            self.assertTrue(json.loads(pause.read_text())["agents"]["worker-node-001"]["worker"]["paused"], stderr)
            self.assertEqual(2, len((root / "runs").read_text().splitlines()))
            process = start()
            try:
                time.sleep(1.2)
            finally:
                stop(process)
            self.assertEqual(2, len((root / "runs").read_text().splitlines()))
            subprocess.run([str(launcher), "resume", "worker"], cwd=project, env=env, check=True, capture_output=True)
            self.assertFalse(json.loads(pause.read_text())["agents"]["worker-node-001"]["worker"]["paused"])

    def test_git_repair_status_returns_to_provider_instead_of_pausing(self):
        for initially_pending in (False, True):
            with self.subTest(initially_pending=initially_pending), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "sample"
                project.mkdir()
                binaries = root / "bin"
                binaries.mkdir()
                curl = binaries / "curl"
                curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '{\"result\":{\"structuredContent\":{\"projects\":[{\"id\":\"p\",\"name\":\"sample\"}],\"summary\":{\"byStatus\":{\"doing\":1}},\"tasks\":[{\"id\":\"t\",\"status\":\"doing\"}]}}}'\n")
                provider = binaries / "claude"
                provider.write_text('#!/usr/bin/env bash\nprintf "run\\n" >>"$RALPH_TEST_DIR/runs"\n')
                curl.chmod(0o755)
                provider.chmod(0o755)
                env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "RALPH_TEST_DIR": directory}
                launcher, runtime = install_global(root / "share", root / "launchers")
                subprocess.run([str(launcher), "init"], cwd=project, env=env, check=True, capture_output=True)
                config = project / ".ralph/config.json"
                settings = json.loads(config.read_text())
                settings["github"]["enabled"] = True
                settings["retry"]["initialSeconds"] = 1
                settings["pollIntervalSeconds"] = 1
                config.write_text(json.dumps(settings))
                (project / ".ralph/mcp.json").write_text('{"mcpServers": {}}')
                (runtime / "lib/github_workflow.py").write_text(
                    "import json,os,sys\nfrom pathlib import Path\n"
                    "root=Path(os.environ['RALPH_TEST_DIR']); project=root/'sample'; marker=root/'repair'; done=root/'done'\n"
                    "command=sys.argv[1]\n"
                    "if command=='prepare':\n"
                    f" print(json.dumps({{'cwd':str(project),'pending':{initially_pending!r} and not marker.exists(),'workerRepair':{{'reason':'whitespace'}}}}))\n"
                    "elif command=='finish':\n"
                    " if not marker.exists():\n"
                    "  marker.touch(); raise SystemExit(76)\n"
                    " done.touch()\n")
                process = subprocess.Popen([str(launcher), "run", "worker"], cwd=project, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline and not (root / "done").exists():
                        time.sleep(0.05)
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                self.assertTrue((root / "done").exists(), stdout + stderr)
                self.assertFalse((project / ".ralph/pauses/worker.json").exists())
                runs = (root / "runs").read_text().splitlines()
                self.assertGreaterEqual(len(runs), 1 if initially_pending else 2)
                self.assertIn("workerへ再判定・修正を返します", stdout)

    def test_operation_recovery_uses_diagnostic_prompt_and_does_not_pause_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "sample"
            project.mkdir()
            binaries = root / "bin"
            binaries.mkdir()
            curl = binaries / "curl"
            curl.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '{\"result\":{\"structuredContent\":{\"projects\":[{\"id\":\"p\",\"name\":\"sample\"}],\"summary\":{\"byStatus\":{\"doing\":1}},\"tasks\":[{\"id\":\"t\",\"status\":\"doing\"}]}}}'\n")
            provider = binaries / "claude"
            provider.write_text("#!/usr/bin/env bash\ncase \"$*\" in *Ralphの操作エラーからの復旧判断*) touch \"$RALPH_TEST_DIR/diagnostic\";; esac\n")
            curl.chmod(0o755)
            provider.chmod(0o755)
            env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "RALPH_TEST_DIR": directory}
            launcher, runtime = install_global(root / "share", root / "launchers")
            subprocess.run([str(launcher), "init"], cwd=project, env=env, check=True, capture_output=True)
            config = project / ".ralph/config.json"
            settings = json.loads(config.read_text())
            settings["github"]["enabled"] = True
            settings["pollIntervalSeconds"] = 1
            settings["retry"]["initialSeconds"] = 1
            config.write_text(json.dumps(settings))
            (project / ".ralph/mcp.json").write_text('{"mcpServers": {}}')
            (runtime / "lib/github_workflow.py").write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "root=Path(os.environ['RALPH_TEST_DIR']); project=root/'sample'; done=root/'done'\n"
                "if sys.argv[1]=='prepare': print(json.dumps({'idle':True} if done.exists() else {'cwd':str(project),'pending':False,'recovery':True,'recoveryTimeoutSeconds':5}))\n"
                "elif sys.argv[1]=='recover': done.touch(); raise SystemExit(79)\n")
            process = subprocess.Popen([str(launcher), "run", "worker"], cwd=project, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not (root / "done").exists():
                    time.sleep(0.05)
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
            self.assertTrue((root / "done").exists(), stdout + stderr)
            self.assertTrue((root / "diagnostic").exists(), stdout + stderr)
            state = json.loads((project / ".ralph/runtime.json").read_text())
            self.assertFalse(state["agents"]["worker-node-001"]["worker"].get("paused", False))

    def test_mcp_error_is_not_treated_as_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text('{"wacha":{"url":"http://unused"}}')
            curl = root / "curl"
            curl.write_text('''#!/usr/bin/env bash
printf '%s\n' '{"result":{"isError":true,"structuredContent":{"error":{"code":"FORBIDDEN"}}}}'
''')
            curl.chmod(0o755)
            backend = Path(__file__).resolve().parents[1] / "ralph/backends/wacha.sh"
            script = 'set -euo pipefail; RALPH_CONFIG_PATH="$1"; WACHA_AGENT_NAME=worker; ralph_log_error() { printf "$@" >&2; }; source "$2"; mcp_call list_tasks "{}"'
            result = subprocess.run(["bash", "-c", script, "bash", str(config), str(backend)],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("FORBIDDEN", result.stderr)


if __name__ == "__main__":
    unittest.main()
