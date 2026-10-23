#!/usr/bin/env python3
"""Append Ralph streams directly to dated files without rename-based rotation."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import queue
import sys
import threading
import time


JST = timezone(timedelta(hours=9))


class DailyLogHandler(logging.Handler):
    def __init__(self, path: Path, backup_count: int):
        super().__init__()
        self.path = path
        self.backup_count = backup_count
        self.last_cleanup = None

    def dated_path(self, date: str) -> Path:
        return self.path.with_name(f"{self.path.stem}.{date}{self.path.suffix}")

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.fromtimestamp(record.created, JST).date()
        path = self.dated_path(day.isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            data = (self.format(record) + "\n").encode("utf-8")
            while data:
                written = os.write(descriptor, data)
                if not written:
                    raise OSError("Log append wrote no bytes")
                data = data[written:]
        finally:
            os.close(descriptor)
        if self.last_cleanup != day:
            self.prune(day)
            self.last_cleanup = day

    def prune(self, today) -> None:
        # No rename and no shared open file handle. Concurrent deletion is idempotent.
        prefix, suffix = self.path.stem + ".", self.path.suffix
        for path in self.path.parent.iterdir():
            if not path.name.startswith(prefix) or not path.name.endswith(suffix) or not path.is_file():
                continue
            name = path.name[len(prefix):len(path.name) - len(suffix)] if suffix else path.name[len(prefix):]
            try:
                date = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if name != date.isoformat():
                continue
            if date < today - timedelta(days=self.backup_count):
                path.unlink(missing_ok=True)


def create_handler(path: Path, backup_count: int = 20) -> DailyLogHandler:
    if isinstance(backup_count, bool) or not isinstance(backup_count, int) or backup_count < 1:
        raise ValueError("logging.backupCount must be a positive integer")
    os.environ["TZ"] = "JST-9"
    time.tzset()
    handler = DailyLogHandler(path, backup_count)
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
