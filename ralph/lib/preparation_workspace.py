"""Execution-local editable preparation files; the product checkout stays pinned."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
import subprocess

if __package__:
    from .github_workflow import WorkflowError, atomic_json, run
else:
    from github_workflow import WorkflowError, atomic_json, run


def preparation_files(config: dict) -> list[str]:
    section = config.get("verification", {})
    if not isinstance(section, dict):
        raise WorkflowError("verification must be an object")
    value = section.get("preparationFiles", [])
    if not isinstance(value, list) or any(not isinstance(name, str) or not name or "\0" in name or
            Path(name).is_absolute() or ".." in Path(name).parts or Path(name).as_posix() != name or
            any(part in {".git", ".ralph"} for part in Path(name).parts) for name in value):
        raise WorkflowError("verification.preparationFiles must contain exact Repository-relative preparation file paths")
    return sorted(set(value))


def ensure_preparation(workflow) -> dict | None:
    files = preparation_files(workflow.config)
    if workflow.role not in {"reviewer", "manager"} or not files:
        return None
    cwd = Path(workflow.state["cwd"])
    head = run("git", "rev-parse", "HEAD", cwd=cwd)
    revision = workflow.config.get("verification", {}).get("preparationRevision", head)
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise WorkflowError("verification.preparationRevision must be a full commit SHA")
    source = run("git", "rev-parse", revision + "^{commit}", cwd=cwd)
    claim = workflow.state.get("worktreeClaimId", workflow.state["claimId"])
    area = workflow.root / ".ralph/verification" / claim
    directory = area / "preparation"
    control = area / "preparation-control.json"
    if area.resolve() != area.absolute() or directory.is_symlink() or control.is_symlink():
        raise WorkflowError("Preparation area is redirected")
    if control.exists():
        record = json.loads(control.read_text())
        if record.get("headSha") != head or record.get("sourceSha") != source or record.get("files") != files or record.get("directory") != str(directory):
            raise WorkflowError("Preparation source SHA or Project boundary changed; retain existing preparation for reassessment")
        return record
    if directory.exists():
        raise WorkflowError("Preparation directory exists without its ownership record; retained")
    # Resolve every source before writing anything. Source is the pinned Git blob,
    # never a potentially stale fixed Project checkout or Agent-modified worktree file.
    sources = {}
    for name in files:
        metadata = run("git", "ls-tree", source, "--", name, cwd=cwd).split()
        if not metadata or metadata[0] not in {"100644", "100755"}:
            raise WorkflowError("Preparation file must be a regular tracked file in the pinned revision: " + name)
        data = subprocess.check_output(["git", "show", source + ":" + name], cwd=cwd)
        sources[name] = (data, 0o755 if metadata[0] == "100755" else 0o644)
    directory.mkdir(parents=True)
    for name, (data, mode) in sources.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    record = {"directory": str(directory), "headSha": head, "sourceSha": source, "files": files,
        "instruction": "Only these execution-local preparation copies may be edited. Product files, AC, expected results, business authorization, runtime state and host command allowlists stay fixed. Record reasons/diffs and rerun affected verification."}
    atomic_json(control, record)
    return record


def preparation_snapshot(workflow) -> dict | None:
    workspace = ensure_preparation(workflow)
    if workspace is None:
        return None
    directory = Path(workspace["directory"])
    declared = set(workspace["files"])
    parents = {parent.as_posix() for name in declared for parent in Path(name).parents if parent != Path(".")}
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink() or (path.is_dir() and name not in parents) or (not path.is_dir() and name not in declared):
            raise WorkflowError("Unexpected/redirected file in preparation workspace: " + name)
    digest = hashlib.sha256()
    contents = {}
    changes = []
    patches = []
    cwd = Path(workflow.state["cwd"])
    for name in workspace["files"]:
        path = directory / name
        if not path.is_file() or path.resolve() != path.absolute():
            raise WorkflowError("Preparation file is missing or redirected: " + name)
        current = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        digest.update(name.encode() + b"\0" + str(mode).encode() + b"\0" + current)
        original = subprocess.check_output(["git", "show", workspace["sourceSha"] + ":" + name], cwd=cwd)
        original_mode = run("git", "ls-tree", workspace["sourceSha"], "--", name, cwd=cwd).split()[0]
        if current != original or mode != (0o755 if original_mode == "100755" else 0o644):
            changes.append(name)
            patches.extend(difflib.unified_diff(original.decode("utf-8", "replace").splitlines(True),
                current.decode("utf-8", "replace").splitlines(True), fromfile="pinned/" + name, tofile="preparation/" + name))
        contents[name] = current
    fingerprint = digest.hexdigest()
    archive = directory.parent / "preparation-evidence" / fingerprint
    if archive.is_symlink() or archive.resolve() != archive.absolute():
        raise WorkflowError("Preparation evidence directory is redirected")
    archive.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        target = archive / "files" / name
        if target.is_symlink() or target.parent.resolve() != target.parent.absolute():
            raise WorkflowError("Preparation evidence file is redirected")
        if target.exists() and target.read_bytes() != data:
            raise WorkflowError("Preparation evidence changed after recording")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (archive / "changes.patch").write_text("".join(patches))
    result = {"directory": str(directory), "headSha": workspace["headSha"], "sourceSha": workspace["sourceSha"], "fingerprint": fingerprint,
        "changedFiles": changes, "patchPath": str(archive / "changes.patch"), "snapshotDirectory": str(archive / "files")}
    atomic_json(archive / "manifest.json", result)
    return result


def preparation_command(workflow, command: list[str]) -> list[str]:
    workspace = ensure_preparation(workflow)
    if workspace is None:
        return command
    cwd = Path(workflow.state["cwd"])
    replacements = {}
    for name in workspace["files"]:
        replacement = str(Path(workspace["directory"]) / name)
        for source in (str(workflow.root / name), str(cwd / name), name, "./" + name):
            replacements[source] = replacement
    return [replacements.get(value, value) for value in command]


def assert_preparation_verified(workflow) -> dict | None:
    current = preparation_snapshot(workflow)
    if current is None:
        return None
    if not current["changedFiles"]:
        return current
    # A preparation repair invalidates earlier verification.
    for path in (workflow.root / ".ralph/verification" / workflow.state["claimId"]).rglob("*.json"):
        if path.name == "manifest.json":
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (record.get("claimId") == workflow.state["claimId"] and record.get("headSha") == current["headSha"]
                and record.get("sourceUnchanged") is True and record.get("preparationUnchanged") is True
                and record.get("preparationWorkspace") == current
                and record.get("status") in {"passed", "stopped"}):
            return current
    raise WorkflowError("Preparation requires fresh successful verification for its current fingerprint and pinned product SHA")
