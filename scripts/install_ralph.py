#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ralph"
RUNTIME_ENTRIES = ("bin", "lib", "backends", "providers", "prompts")


def install(target: Path) -> tuple[Path, bool]:
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"Target directory does not exist: {target}")

    ralph_dir = target / ".ralph"
    runtime_dir = ralph_dir / "runtime"
    ralph_dir.mkdir(exist_ok=True)

    for entry in RUNTIME_ENTRIES:
        source = SOURCE / entry
        if source.exists():
            shutil.copytree(source, runtime_dir / entry, dirs_exist_ok=True)

    executable = runtime_dir / "bin" / "ralph-loop"
    executable.chmod(executable.stat().st_mode | 0o111)

    config_path = ralph_dir / "config.json"
    created_config = not config_path.exists()
    if created_config:
        config = json.loads((SOURCE / "examples" / "config.json").read_text(encoding="utf-8"))
        config["projectName"] = target.name
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return executable, created_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Ralph Runner into a project.")
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()

    try:
        executable, created_config = install(args.target)
    except ValueError as error:
        parser.error(str(error))

    print(f"installed Ralph runtime: {executable.parent.parent}")
    if created_config:
        print(f"created config: {executable.parents[2] / 'config.json'}")
    else:
        print(f"kept existing config: {executable.parents[2] / 'config.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
