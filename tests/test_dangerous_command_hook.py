import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".apm/hooks/scripts/check-dangerous-command.py"


def run_hook(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=False,
    )


class DangerousCommandHookTest(unittest.TestCase):
    def assert_blocked(self, command: str) -> None:
        result = run_hook(command)
        self.assertEqual(0, result.returncode)
        response = json.loads(result.stdout)
        self.assertFalse(response["continue"])
        self.assertIn("using its captured PID", response["stopReason"])

    def assert_allowed(self, command: str) -> None:
        result = run_hook(command)
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_blocks_process_wide_termination(self):
        for command in (
            'pkill -f "tsx src/server.ts"',
            "cd /tmp && /usr/bin/pkill -f node",
            "sudo killall node",
            "env MATCH=server killall node",
            'kill "$(pgrep -f server.ts)"',
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_allows_pid_scoped_cleanup_and_text_search(self):
        for command in (
            'kill "$server_pid"',
            "kill 12345",
            "printf 'pkill is forbidden\\n'",
            "rg -n 'pkill|killall' .",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)


if __name__ == "__main__":
    unittest.main()
