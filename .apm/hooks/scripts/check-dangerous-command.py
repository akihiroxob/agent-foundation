#!/usr/bin/env python3
import json
import sys

BLOCKED = (
    "rm -rf /",
    "git reset --hard",
    "git clean -fd",
)

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

command = str(payload.get("tool_input", {}).get("command", ""))
if any(fragment in command for fragment in BLOCKED):
    print(json.dumps({
        "continue": False,
        "stopReason": f"Blocked dangerous command: {command}",
    }))

sys.exit(0)
