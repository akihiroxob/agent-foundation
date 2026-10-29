from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ralph.lib.log_stream import create_handler

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))


class LogStreamTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "ralph.log"
        self.previous_tz = os.environ.get("TZ")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.restore_timezone)

    def restore_timezone(self):
        if self.previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous_tz
        time.tzset()

    def handler(self, at, backup_count=20):
        self.path.touch()
        os.utime(self.path, (at.timestamp(), at.timestamp()))
        with patch("ralph.lib.log_stream.time.time", return_value=at.timestamp()):
            handler = create_handler(self.path, backup_count)
        self.addCleanup(handler.close)
        return handler

    @staticmethod
    def emit(handler, at, message):
        with patch("ralph.lib.log_stream.time.time", return_value=at.timestamp()):
            record = logging.LogRecord("ralph", logging.INFO, "", 0, message, (), None)
            record.role = "worker"
            handler.handle(record)

    def daily(self, at):
        return self.root / f"ralph.{at.astimezone(JST):%Y-%m-%d}.log"

    def test_midnight_appends_to_distinct_dates_with_two_writers(self):
        before = datetime(2026, 10, 10, 23, 59, 59, tzinfo=JST)
        one, two = self.handler(before), self.handler(before)
        self.emit(one, before, "A before")
        self.emit(two, before, "B before")
        after = before + timedelta(seconds=1)
        self.emit(one, after, "A after")
        self.emit(two, after, "B after")
        self.assertIn("A before", self.daily(before).read_text())
        self.assertIn("B before", self.daily(before).read_text())
        self.assertIn("A after", self.daily(after).read_text())
        self.assertIn("B after", self.daily(after).read_text())

    def test_retains_twenty_days_and_preserves_unrelated_files(self):
        at = datetime(2026, 1, 1, 12, tzinfo=JST)
        one, two = self.handler(at), self.handler(at)
        (self.root / "ralph.notes.log").write_text("notes")
        for day in range(25):
            self.emit(one, at + timedelta(days=day), f"A-{day}")
            self.emit(two, at + timedelta(days=day), f"B-{day}")
        files = sorted(self.root.glob("ralph.2026-*.log"))
        self.assertEqual(21, len(files))  # current day + 20 prior days
        self.assertEqual("ralph.2026-01-05.log", files[0].name)
        self.assertEqual("notes", (self.root / "ralph.notes.log").read_text())

    def test_restart_preserves_legacy_file_and_appends_daily_file(self):
        at = datetime(2026, 1, 1, 12, tzinfo=JST)
        self.path.write_text("existing legacy history")
        one = self.handler(at)
        self.emit(one, at, "first")
        one.close()
        two = self.handler(at)
        self.emit(two, at, "second")
        self.assertEqual(2, len(self.daily(at).read_text().splitlines()))
        self.assertEqual("existing legacy history", self.path.read_text())

    def test_regular_streams_are_drained_and_keep_separate_console_destinations(self):
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        role = self.root / "role"
        stdout.write_text("".join(f"out-{i}\n" for i in range(200)) + "unterminated")
        stderr.write_text("".join(f"err-{i}\n" for i in range(200)))
        role.write_text("reviewer")
        # Force rotation while both streams are producing output.
        previous = time.time() - 86400
        self.path.write_text("legacy history\n")
        os.utime(self.path, (previous, previous))
        result = subprocess.run(self.command(stdout, stderr, role), capture_output=True, text=True, check=True, timeout=10)
        suffix = datetime.fromtimestamp(previous, JST).strftime("%Y-%m-%d")
        self.assertEqual("legacy history\n", self.path.read_text())
        lines = self.daily(datetime.now(JST)).read_text().splitlines()
        self.assertEqual(401, len(lines))
        messages = [line.split("][reviewer] ", 1)[1] for line in lines]
        self.assertEqual(sorted([f"out-{i}" for i in range(200)] + [f"err-{i}" for i in range(200)] + ["unterminated"]), sorted(messages))
        self.assertNotIn("err-", result.stdout)
        self.assertNotIn("out-", result.stderr)
        self.assertIn("unterminated", result.stdout)
        self.assertEqual(0o600, self.daily(datetime.now(JST)).stat().st_mode & 0o777)

    def command(self, stdout, stderr, role):
        return [sys.executable, str(ROOT / "ralph/lib/log_stream.py"), "--log", str(self.path),
            "--stdout-pipe", str(stdout), "--stderr-pipe", str(stderr), "--role-file", str(role),
            "--ready-file", str(self.root / "ready")]

    def test_fifo_role_changes_and_trailing_lines_are_preserved_on_shutdown(self):
        stdout, stderr, role = (self.root / name for name in ("stdout", "stderr", "role"))
        os.mkfifo(stdout)
        os.mkfifo(stderr)
        role.write_text("worker")
        process = subprocess.Popen(self.command(stdout, stderr, role), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        with stdout.open("w") as out, stderr.open("w") as err:
            out.write("first\n")
            out.flush()
            first = process.stdout.readline()
            self.assertIn("][worker] first", first)
            role.write_text("manager")
            err.write("last without newline")
        out_text, err_text = process.communicate(timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertEqual("", out_text)
        self.assertIn("][manager] last without newline", err_text)
        self.assertEqual(first + err_text, self.daily(datetime.now(JST)).read_text())

    def test_closed_console_does_not_stop_file_logging(self):
        stdout, stderr, role = (self.root / name for name in ("stdout", "stderr", "role"))
        stdout.write_text("line\n" * 200)
        stderr.write_text("")
        role.write_text("worker")
        process = subprocess.Popen(self.command(stdout, stderr, role), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.stdout.close()
        self.assertEqual(0, process.wait(timeout=5))
        self.assertEqual("", process.stderr.read())
        process.stderr.close()
        self.assertEqual(200, len(self.daily(datetime.now(JST)).read_text().splitlines()))

    def test_invalid_retention_is_rejected(self):
        for count in (0, -1, True, 1.5, "20"):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "positive integer"):
                create_handler(self.path, count)

    def test_log_io_failure_is_not_silently_ignored(self):
        at = datetime(2026, 1, 1, 23, 59, tzinfo=JST)
        handler = self.handler(at)
        with patch("ralph.lib.log_stream.os.open", side_effect=OSError("append failed")):
            with self.assertRaisesRegex(OSError, "append failed"):
                self.emit(handler, at, "must not vanish silently")

    def test_multiple_processes_append_without_renaming_or_losing_records(self):
        script = """import logging,sys
from pathlib import Path
from ralph.lib.log_stream import create_handler
h=create_handler(Path(sys.argv[1]))
for i in range(200):
 r=logging.LogRecord('ralph',20,'',0,sys.argv[2]+':'+str(i),(),None)
 r.role='worker:'+sys.argv[2]
 h.handle(r)
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.path), identity], cwd=ROOT)
                     for identity in ("a", "b", "c")]
        for process in processes:
            self.assertEqual(0, process.wait(timeout=10))
        lines = self.daily(datetime.now(JST)).read_text().splitlines()
        self.assertEqual(600, len(lines))
        self.assertEqual(600, len(set(lines)))


if __name__ == "__main__":
    unittest.main()
