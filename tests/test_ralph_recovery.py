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
            pause = project / ".ralph/pauses/worker.json"

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
                    if pause.exists() and json.loads(pause.read_text()).get("paused"):
                        break
                    time.sleep(0.05)
            finally:
                _, stderr = stop(process)
            self.assertTrue(json.loads(pause.read_text())["paused"], stderr)
            self.assertEqual(2, len((root / "runs").read_text().splitlines()))
            process = start()
            try:
                time.sleep(1.2)
            finally:
                stop(process)
            self.assertEqual(2, len((root / "runs").read_text().splitlines()))
            subprocess.run([str(launcher), "resume", "worker"], cwd=project, env=env, check=True, capture_output=True)
            self.assertFalse(pause.exists())

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
