#!/usr/bin/env python3
"""Ephemeral Git credential helper. Called only by the host Git subprocess."""
from __future__ import annotations

import os
import sys


def main() -> int:
    # Never store/erase credentials or print secrets for unrelated operations.
    if len(sys.argv) != 2 or sys.argv[1] != "get":
        return 0
    fields = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    token = os.environ.get("RALPH_GIT_AUTH_TOKEN", "")
    repository = os.environ.get("RALPH_GIT_REPOSITORY", "")
    if (fields.get("protocol") != "https" or fields.get("host") != "github.com" or
            not repository or fields.get("path", "").lower() != f"{repository}.git".lower() or
            not token or any(char in token for char in "\r\n\0")):
        print("quit=true")
        return 0
    # stdout is a private pipe to Git, never Ralph's user-facing log.
    sys.stdout.write(f"username=x-access-token\npassword={token}\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
