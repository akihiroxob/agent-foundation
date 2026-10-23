#!/usr/bin/env python3
"""Bound one Agent execution and terminate its process group on timeout or cancellation."""
from __future__ import annotations

import os
import signal
import subprocess
import sys


def execute(seconds: int, command: list[str]) -> int:
    process = subprocess.Popen(command, start_new_session=True)
    def terminate(signum, _frame=None):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    def cancel(signum, _frame):
        terminate(signum)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate(signal.SIGKILL)
            process.wait()
        raise SystemExit(128 + signum)
    old = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            return process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            terminate(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate(signal.SIGKILL)
                process.wait()
            return 124
    finally:
        if process.poll() is None:
            terminate(signal.SIGKILL)
            process.wait()
        for sig, handler in old.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    seconds = int(sys.argv[1])
    if seconds < 1 or len(sys.argv) < 3:
        raise SystemExit("Expected positive timeout and Agent command")
    raise SystemExit(execute(seconds, sys.argv[2:]))
