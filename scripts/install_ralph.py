#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ralph"
RUNTIME_ENTRIES = ("bin", "lib", "backends", "providers", "prompts", "examples")


def copy_runtime(runtime_dir: Path) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    for entry in RUNTIME_ENTRIES:
        source = SOURCE / entry
        if source.exists():
            shutil.copytree(source, runtime_dir / entry, dirs_exist_ok=True)

    for command_name in ("ralph", "ralph-loop"):
        command = runtime_dir / "bin" / command_name
        command.chmod(command.stat().st_mode | 0o111)
    return runtime_dir / "bin" / "ralph"


def install_local(target: Path) -> tuple[Path, bool]:
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"Target directory does not exist: {target}")

    ralph_dir = target / ".ralph"
    runtime_dir = ralph_dir / "runtime"
    ralph_dir.mkdir(exist_ok=True)
    executable = copy_runtime(runtime_dir)

    config_path = ralph_dir / "config.json"
    created_config = not config_path.exists()
    if created_config:
        config = json.loads((SOURCE / "examples" / "config.json").read_text(encoding="utf-8"))
        config["projectName"] = target.name
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return executable, created_config


def install_global(data_home: Path | None = None, bin_dir: Path | None = None) -> tuple[Path, Path]:
    resolved_data_home = data_home or Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    resolved_bin_dir = bin_dir or Path(os.environ.get("RALPH_BIN_DIR", Path.home() / ".local" / "bin"))
    runtime_dir = resolved_data_home.expanduser().resolve() / "agent-foundation" / "ralph" / "runtime"
    executable = copy_runtime(runtime_dir)

    resolved_bin_dir = resolved_bin_dir.expanduser().resolve()
    resolved_bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = resolved_bin_dir / "ralph"
    launcher.write_text(
        f"#!/usr/bin/env bash\nexec {shlex.quote(str(executable))} \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, runtime_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Ralph Runner.")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--target", type=Path, help="Install and pin Ralph in a repository.")
    destination.add_argument("--global", dest="install_global", action="store_true", help="Install shared Ralph CLI.")
    args = parser.parse_args()

    if args.install_global:
        launcher, runtime_dir = install_global()
        print(f"installed Ralph runtime: {runtime_dir}")
        print(f"installed command: {launcher}")
        if launcher.parent not in [Path(path).expanduser().resolve() for path in os.environ.get("PATH", "").split(os.pathsep) if path]:
            print(f"add to PATH if necessary: {launcher.parent}")
    else:
        try:
            executable, created_config = install_local(args.target)
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
