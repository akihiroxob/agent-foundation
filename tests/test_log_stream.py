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

    def test_midnight_in_japan_rotates_without_splitting_lines(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        before = datetime(2026, 10, 10, 23, 59, 59, tzinfo=JST)
        handler = self.handler(before)
        self.emit(handler, before, "日本語 before midnight")
        self.emit(handler, before + timedelta(seconds=1), "after midnight")
        old = (self.root / "ralph.log.2026-10-10").read_text()
        self.assertEqual("[2026-10-10 23:59:59][worker] 日本語 before midnight\n", old)
        self.assertEqual("[2026-10-11 00:00:00][worker] after midnight\n", self.path.read_text())

    def test_retains_twenty_backups_and_preserves_unrelated_files(self):
        at = datetime(2026, 1, 1, 12, tzinfo=JST)
        handler = self.handler(at)
        (self.root / "verification.json").write_text("evidence")
        (self.root / "ralph.log.notes").write_text("notes")
        for day in range(25):
            self.emit(handler, at + timedelta(days=day), f"day-{day}")
        backups = sorted(self.root.glob("ralph.log.2026-*"))
        self.assertEqual(20, len(backups))
        self.assertEqual("ralph.log.2026-01-05", backups[0].name)
        self.assertEqual("ralph.log.2026-01-24", backups[-1].name)
        self.assertIn("day-24", self.path.read_text())
        self.assertEqual("evidence", (self.root / "verification.json").read_text())
        self.assertEqual("notes", (self.root / "ralph.log.notes").read_text())

    def test_restart_appends_and_rotates_after_idle_days(self):
        at = datetime(2026, 1, 1, 12, tzinfo=JST)
        self.path.write_text("existing history\n")
        handler = self.handler(at)
        self.emit(handler, at, "first execution")
        handler.close()
        # Simulate the existing log mtime across process restarts.
        next_day = at + timedelta(days=1)
        os.utime(self.path, (at.timestamp(), at.timestamp()))
        with patch("ralph.lib.log_stream.time.time", return_value=next_day.timestamp()):
            restarted = create_handler(self.path)
        self.addCleanup(restarted.close)
        self.emit(restarted, next_day, "restarted")
        self.assertIn("existing history\n", (self.root / "ralph.log.2026-01-01").read_text())
        self.emit(restarted, at + timedelta(days=5), "after idle days")
        self.assertIn("restarted", (self.root / "ralph.log.2026-01-02").read_text())
        self.assertFalse((self.root / "ralph.log.2026-01-03").exists())
        self.assertIn("after idle days", self.path.read_text())

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
        self.assertEqual("legacy history\n", (self.root / f"ralph.log.{suffix}").read_text())
        lines = self.path.read_text().splitlines()
        self.assertEqual(401, len(lines))
        messages = [line.split("][reviewer] ", 1)[1] for line in lines]
        self.assertEqual(sorted([f"out-{i}" for i in range(200)] + [f"err-{i}" for i in range(200)] + ["unterminated"]), sorted(messages))
        self.assertNotIn("err-", result.stdout)
        self.assertNotIn("out-", result.stderr)
        self.assertIn("unterminated", result.stdout)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

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
        self.assertEqual(first + err_text, self.path.read_text())

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
        self.assertEqual(200, len(self.path.read_text().splitlines()))

    def test_invalid_retention_is_rejected(self):
        for count in (0, -1, True, 1.5, "20"):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "positive integer"):
                create_handler(self.path, count)

    def test_rotation_io_failure_is_not_silently_ignored(self):
        at = datetime(2026, 1, 1, 23, 59, tzinfo=JST)
        handler = self.handler(at)
        with patch.object(handler, "doRollover", side_effect=OSError("rotation failed")):
            with self.assertRaisesRegex(OSError, "rotation failed"):
                self.emit(handler, at + timedelta(minutes=2), "must not vanish silently")


if __name__ == "__main__":
    unittest.main()
