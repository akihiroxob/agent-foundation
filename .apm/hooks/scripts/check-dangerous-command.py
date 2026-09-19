#!/usr/bin/env python3
import json
import re
import sys

BLOCKED = (
    "rm -rf /",
    "git reset --hard",
    "git clean -fd",
)

PROCESS_WIDE_TERMINATION = (
    re.compile(
        r"(?:^|(?:&&|\|\||[;|&(\n])\s*)"
        r"(?:sudo(?:\s+-\S+)*\s+|command\s+|env(?:\s+\w+=\S+)*\s+)?"
        r"(?:/[^\s;&|]+/)?(?:pkill|killall)(?=\s|$)"
    ),
    re.compile(r"(?:^|[;&|\n]\s*)kill\b[^\n;&|]*\$\(\s*pgrep\b"),
)

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

command = str(payload.get("tool_input", {}).get("command", ""))
if any(fragment in command for fragment in BLOCKED) or any(
    pattern.search(command) for pattern in PROCESS_WIDE_TERMINATION
):
    print(json.dumps({
        "continue": False,
        "stopReason": (
            f"Blocked dangerous command: {command}. "
            "Stop only a verification process started by this Agent, using its captured PID."
        ),
    }))

sys.exit(0)
