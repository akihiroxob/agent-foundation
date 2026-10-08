#!/usr/bin/env python3
"""Parse provider reset times without exposing captured output."""
from __future__ import annotations

import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def retry_seconds(text: str, now: datetime | None = None) -> int:
    match = list(re.finditer(
        r"(?:resets\s+|try again at\s+)(\d{1,2}:\d{2}\s*(?:am|pm))(?:\s*\(([^()]+)\))?",
        text, re.IGNORECASE,
    ))
    if not match:
        raise ValueError("No reset time")
    time_text, timezone_name = match[-1].groups()
    timezone = ZoneInfo(timezone_name.strip()) if timezone_name else None
    now = now.astimezone(timezone) if now else (datetime.now(timezone) if timezone else datetime.now().astimezone())
    parsed = datetime.strptime(re.sub(r"\s+", "", time_text).upper(), "%I:%M%p")
    reset = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return max(1, math.ceil((reset - now).total_seconds()) + 60)


if __name__ == "__main__":
    try:
        text = "\n".join(Path(path).read_text(encoding="utf-8", errors="replace") for path in sys.argv[1:])
        print(retry_seconds(text))
    except (ValueError, OSError):
        raise SystemExit(1)
