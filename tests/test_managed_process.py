from __future__ import annotations
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ManagedProcessTest(unittest.TestCase):
    def test_interrupt_terminates_sleep_and_releases_inherited_lock(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as directory:
                self.check_shutdown(Path(directory), signum, False)

    def test_forces_stubborn_descendant_within_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            self.check_shutdown(Path(directory), signal.SIGINT, True)

    def check_shutdown(self, directory, signum, stubborn):
        ready = directory / "ready"
        lock = directory / "lock"
        child = directory / "child.py"
        child.write_text("import signal,time,sys\nfrom pathlib import Path\n" +
            ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\n" if stubborn else "") +
            "Path(sys.argv[1]).write_text('ready')\ntime.sleep(120)\n")
        wrapper = """import os,fcntl,sys
from ralph.lib.managed_process import run_managed
fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
try:
 code=run_managed(['bash','-c',sys.argv[4]],pass_fds=(fd,),grace_seconds=.5)
finally: os.close(fd)
sys.exit(code)
"""
        command = f"{sys.executable} {child} {ready}; sleep 120"
        process = subprocess.Popen([sys.executable, "-c", wrapper, str(lock), str(child), str(ready), command],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            end = time.monotonic() + 4
            while not ready.exists() and time.monotonic() < end:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            started = time.monotonic()
            process.send_signal(signum)
            process.communicate(timeout=3)
            self.assertEqual(128 + signum, process.returncode)
            self.assertLess(time.monotonic()-started, 2)
            fd = os.open(lock,os.O_RDWR)
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally: os.close(fd)
        finally:
            if process.poll() is None: process.kill();process.communicate()
