"""Principal/Role identity, OS locks and transactional local operational state."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote

ROLES = ("manager", "reviewer", "worker")


def component(principal: str) -> str:
    if not isinstance(principal, str) or not principal.strip() or principal != principal.strip() or len(principal) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in principal):
        raise ValueError("Principal must be a non-empty identifier without surrounding whitespace/control characters")
    # Encode ASCII uppercase bytes too, preserving distinct IDs on case-insensitive macOS.
    value = "".join(f"%{ord(c):02X}" if "A" <= c <= "Z" else quote(c, safe="@-_") for c in principal)
    return value if value not in (".", "..") else value.replace(".", "%2E")


def project_root(config_path: Path, config: dict) -> Path:
    base = config_path.resolve().parent.parent
    return (base / config.get("projectRoot", ".")).resolve()


def principal_for(config: dict, role: str) -> str:
    identities = json.loads(os.environ.get("RALPH_ROLE_PRINCIPALS", "{}"))
    principal = identities.get(role, config["roles"][role]["agentName"])
    component(principal)
    return principal


def execution_path(root: Path, principal: str, role: str) -> Path:
    return root / ".ralph/github-state" / component(principal) / f"{role}.json"


def worktree_path(root: Path, principal: str, role: str) -> Path:
    return root / ".ralph/worktrees" / component(principal) / role


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


@contextmanager
def transaction(root: Path):
    directory = root / ".ralph"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / "runtime.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = read_object(directory / "runtime.json")
        previous = json.dumps(state, sort_keys=True)
        version = state.setdefault("schemaVersion", 1)
        if version != 1:
            raise ValueError(f"Unsupported runtime schemaVersion: {version}")
        state.setdefault("agents", {})
        state.setdefault("quotas", {})
        if not isinstance(state["agents"], dict) or not isinstance(state["quotas"], dict):
            raise ValueError("runtime.json agents and quotas must be objects")
        yield state
        if json.dumps(state, sort_keys=True) != previous:
            atomic_json(directory / "runtime.json", state)
    finally:
        os.close(descriptor)


def legacy_runner_check(root: Path) -> None:
    path = root / ".ralph/runner.pid"
    if not path.exists():
        return
    try:
        pid = int(path.read_text())
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return
    except (ValueError, PermissionError) as error:
        raise ValueError("Legacy Ralph runner.pid cannot be verified; stop the old runner before upgrading") from error
    raise ValueError(f"Legacy Ralph is still running (PID {pid}); stop it before starting the new runner")


def migrate(root: Path, config: dict) -> None:
    # The fixed OS lock serializes the whole migration, including JSON replacement.
    with transaction(root) as state:
        legacy_runner_check(root)
        if state.get("legacyImported"):
            return
        for role in ROLES:
            if role not in config.get("roles", {}):
                continue
            principal = config["roles"][role]["agentName"]
            target = execution_path(root, principal, role)
            old = root / ".ralph/github-state" / f"{role}.json"
            backup = old.with_suffix(".json.migrated")
            source = old if old.exists() else backup
            if source.exists():
                previous = read_object(source)
                previous.update(principal=principal, role=role, legacyWorktree=True)
                if target.exists() and read_object(target) != previous:
                    raise ValueError(f"Both legacy and Principal execution states exist for {principal}/{role}")
                atomic_json(target, previous)
                # Retain the original record as a migration backup, never discard work.
                if old.exists():
                    old.rename(backup)
            for record in (root / ".ralph/github-state/completed").glob(f"{role}-*.json"):
                destination = target.parent / "completed" / record.name
                if not destination.exists():
                    execution = read_object(record)
                    execution.update(principal=principal, role=role, legacyWorktree=True)
                    atomic_json(destination, execution)
            pause = root / ".ralph/pauses" / f"{role}.json"
            if pause.exists():
                state["agents"].setdefault(principal, {}).setdefault(role, read_object(pause))
        old_quota = root / ".ralph/quota.json"
        if old_quota.exists():
            for key, until in read_object(old_quota).items():
                state["quotas"][key] = max(state["quotas"].get(key, 0), until)
        state["legacyImported"] = True


def acquire_roles(root: Path, identities: dict[str, str]) -> list[int]:
    handles = []
    try:
        for role, principal in sorted(identities.items()):
            path = root / ".ralph/runners" / component(principal) / f"{role}.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            handles.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(f"Ralph is already running: principal={principal} role={role}") from error
            os.ftruncate(descriptor, 0)
            os.write(descriptor, str(os.getpid()).encode())
        return handles
    except BaseException:
        for descriptor in handles:
            os.close(descriptor)
        raise


def operate(root: Path, principal: str, role: str, action: str, value: str = "", key: str = ""):
    with transaction(root) as state:
        agent = state["agents"].setdefault(principal, {}).setdefault(role, {})
        now = int(time.time())
        if action == "paused":
            return bool(agent.get("paused", False))
        if action == "pause":
            agent.update(paused=True, reason=value, pausedAt=now)
        elif action == "resume":
            agent.update(paused=False, attempts=0, resumedAt=now, resumeGeneration=agent.get("resumeGeneration", 0) + 1)
        elif action == "progress":
            # Successful work must never clear an explicit/concurrent pause.
            agent["attempts"] = 0
        elif action == "no-progress":
            agent["attempts"] = agent.get("attempts", 0) + 1
            return agent["attempts"]
        elif action == "activity":
            activity = json.loads(value)
            if not isinstance(activity, dict) or activity.get("phase") not in {"agent_running", "agent_finished", "applying", "waiting", "retry_wait"}:
                raise ValueError("Invalid runtime activity")
            agent["activity"] = {**activity, "updatedAt": now}
        elif action == "quota":
            return state["quotas"].get(key, 0)
        elif action == "set-quota":
            state["quotas"][key] = max(state["quotas"].get(key, 0), int(value))
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "principal", "path", "paused", "pause", "resume", "progress", "no-progress", "quota", "set-quota", "activity"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--value", default="")
    parser.add_argument("--key", default="")
    args = parser.parse_args()
    config = read_object(args.config)
    root = project_root(args.config, config)
    principal = principal_for(config, args.role)
    if args.action == "principal":
        result = principal
    elif args.action == "path":
        result = str(execution_path(root, principal, args.role))
    else:
        result = operate(root, principal, args.role, args.action, args.value, args.key)
    if result is not None:
        print(json.dumps(result) if isinstance(result, bool) else result)


if __name__ == "__main__":
    main()
