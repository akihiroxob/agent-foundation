#!/usr/bin/env python3
"""Serialize Ralph stdout/stderr and rotate the shared log at midnight in Japan."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time


class DailyLogHandler(TimedRotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # A failed file write must not be silently reported as successful logging.
        raise


def create_handler(path: Path, backup_count: int = 20) -> DailyLogHandler:
    if isinstance(backup_count, bool) or not isinstance(backup_count, int) or backup_count < 1:
        raise ValueError("logging.backupCount must be a positive integer")
    # Only this dedicated process changes TZ. The Agent/Runner environment is unchanged.
    os.environ["TZ"] = "JST-9"
    time.tzset()
    handler = DailyLogHandler(path, when="midnight", interval=1, backupCount=backup_count,
        encoding="utf-8", utc=False)
    handler.setFormatter(logging.Formatter("[%(asctime)s][%(role)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    return handler


def read_lines(path: Path, stream: str, role_path: Path, events: queue.Queue) -> None:
    role = "auto"
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                current = role_path.read_text(encoding="utf-8").strip()
                if current:
                    role = current
                events.put((stream, line.rstrip("\n"), role, time.time()))
    except Exception as error:
        events.put(error)
    finally:
        events.put(None)


def collect(handler: DailyLogHandler, stdout_pipe: Path, stderr_pipe: Path, role_path: Path) -> None:
    events = queue.Queue(maxsize=1024)
    for path, stream in ((stdout_pipe, "stdout"), (stderr_pipe, "stderr")):
        threading.Thread(target=read_lines, args=(path, stream, role_path, events), daemon=True).start()
    closed = 0
    unavailable = set()
    while closed < 2:
        event = events.get()
        if event is None:
            closed += 1
            continue
        if isinstance(event, Exception):
            raise event
        stream, line, role, created = event
        record = logging.LogRecord("ralph", logging.INFO, "", 0, line, (), None)
        record.created = created
        record.role = role
        handler.handle(record)
        # Keep stdout/stderr destinations separate; file rotation has one owner.
        if stream not in unavailable:
            output = sys.stdout if stream == "stdout" else sys.stderr
            try:
                output.write(handler.format(record) + "\n")
                output.flush()
            except BrokenPipeError:
                unavailable.add(stream)
                # Avoid another EPIPE when Python flushes the console at exit.
                descriptor = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(descriptor, output.fileno())
                finally:
                    os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--stdout-pipe", type=Path, required=True)
    parser.add_argument("--stderr-pipe", type=Path, required=True)
    parser.add_argument("--role-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--backup-count", type=int, default=20)
    args = parser.parse_args()
    os.umask(0o077)
    handler = create_handler(args.log, args.backup_count)
    try:
        args.ready_file.touch(mode=0o600)
        collect(handler, args.stdout_pipe, args.stderr_pipe, args.role_file)
    finally:
        handler.close()


if __name__ == "__main__":
    main()
