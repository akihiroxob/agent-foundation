from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AgentTimeoutTest(unittest.TestCase):
    def test_returns_agent_exit_status(self):
        result = subprocess.run([sys.executable, str(ROOT / 'ralph/lib/agent_timeout.py'), '5',
            sys.executable, '-c', 'raise SystemExit(7)'], check=False)
        self.assertEqual(result.returncode, 7)

    def test_timeout_terminates_agent_and_descendant_holding_output_pipe(self):
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(ROOT / 'ralph/lib/agent_timeout.py'), '1',
            sys.executable, '-c', 'import subprocess,time,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); time.sleep(60)'],
            capture_output=True, timeout=8, check=False)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - start, 8)
