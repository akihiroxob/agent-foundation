"""Bounded shutdown of one Runner process group, retaining execution files."""
from __future__ import annotations

import os
import signal
import subprocess
import time


def run_managed(command, *, env=None, pass_fds=(), grace_seconds=5):
    process = subprocess.Popen(command, env=env, pass_fds=pass_fds, start_new_session=True)
    interrupted = None
    deadline = None
    previous = {}

    def send(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def stop(signum, frame):
        nonlocal interrupted, deadline
        if interrupted is not None:
            deadline = time.monotonic()
            send(signal.SIGKILL)
            return
        interrupted = signum
        deadline = time.monotonic() + grace_seconds
        # Wake the shell trap first; interruptible sleeps avoid waiting for 300 seconds.
        # Do not interrupt unrelated in-flight Python frames with a group-wide INT.
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass

    def group_alive():
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        while process.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                send(signal.SIGKILL)
            time.sleep(.05)
        return 128 + interrupted if interrupted is not None else process.returncode
    finally:
        # Descendants can outlive Bash and hold Role locks or log pipe descriptors.
        send(signal.SIGTERM)
        end = deadline if deadline is not None else time.monotonic() + grace_seconds
        while group_alive() and time.monotonic() < end:
            process.poll()
            time.sleep(.05)
        send(signal.SIGKILL)
        process.wait(timeout=2)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
