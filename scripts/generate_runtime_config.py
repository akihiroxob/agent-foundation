#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.runtime_adapters import render_claude, render_codex, write_files


def generated_files() -> dict[Path, str]:
    policy = json.loads((ROOT / "policy/runtime-policy.json").read_text(encoding="utf-8"))
    outputs: dict[Path, str] = {}
    for target, rendered in (("codex", render_codex(policy)), ("claude", render_claude(policy))):
        for relative, content in rendered.items():
            outputs[Path("dist") / target / relative] = content
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = generated_files()

    if args.check:
        drift = []
        for relative, content in expected.items():
            path = ROOT / relative
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(str(relative))
        if drift:
            print("Generated runtime config is missing or stale:")
            for path in drift:
                print(f"- {path}")
            return 1
        print("Generated runtime config is up to date.")
        return 0

    for relative, content in expected.items():
        write_files(ROOT, {str(relative): content})
        print(f"generated {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
