"""Host-owned preview lifecycle for exploratory Manager browser verification."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

if __package__:
    from .github_workflow import WorkflowError, atomic_json, run
else:
    from github_workflow import WorkflowError, atomic_json, run


def browser_config(config: dict, profile: str | None = None) -> dict | None:
    section = config.get("verification", {})
    if not isinstance(section, dict):
        raise WorkflowError("verification must be an object")
    value = section.get("browser")
    if value is None:
        return None
    if isinstance(value, dict):
        profiles = value.get("profiles", {})
        if not isinstance(profiles, dict) or any(not isinstance(name, str) or not name or not isinstance(entry, dict) or "profiles" in entry for name, entry in profiles.items()):
            raise WorkflowError("verification.browser.profiles must map names to approved preview configurations")
        for entry in profiles.values():
            browser_config({"verification": {"browser": entry}})
        if profile is not None:
            if profile not in profiles:
                raise WorkflowError("Unknown Project-approved browser profile")
            value = profiles[profile]
        else:
            value = {key: item for key, item in value.items() if key != "profiles"}
    if not isinstance(value, dict) or set(value) - {"command", "env", "readyPath", "setupCommands"}:
        raise WorkflowError("verification.browser must contain command, optional env, readyPath and setupCommands")
    command = value.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s or "\0" in s for s in command):
        raise WorkflowError("verification.browser.command must be a non-empty argument array")
    env = value.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not k.isidentifier() or
            k in {"HOME", "PATH", "WACHA_AGENT_NAME", "SSH_AUTH_SOCK"} or "GITHUB" in k or
            not isinstance(v, str) or "\0" in v for k, v in env.items()):
        raise WorkflowError("verification.browser.env must contain explicit preview variables, not runtime credentials")
    path = value.get("readyPath", "/")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\0" in path:
        raise WorkflowError("verification.browser.readyPath must be a local absolute URL path")
    setup = value.get("setupCommands", [])
    if not isinstance(setup, list) or any(not isinstance(item, list) or not item or
            any(not isinstance(arg, str) or not arg or "\0" in arg for arg in item) for item in setup):
        raise WorkflowError("verification.browser.setupCommands must be an array of argument arrays")
    return {"command": command, "env": env, "readyPath": path, "setupCommands": setup}


def playwright_config(root: Path) -> dict:
    path = root / ".mcp.json"
    try:
        value = json.loads(path.read_text())["mcpServers"]["playwright"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise WorkflowError("Install the APM playwright MCP dependency into the Project .mcp.json first") from error
    # Browser-specific constraints belong to preview preparation, not Provider adapters.
    if not isinstance(value, dict) or value.get("type", "stdio") != "stdio" or not isinstance(value.get("command"), str) or not value["command"]:
        raise WorkflowError("Project playwright MCP must be a stdio command")
    args = value.get("args", [])
    if not isinstance(args, list) or any(not isinstance(s, str) or "\0" in s for s in args):
        raise WorkflowError("Project playwright MCP args must be an argument array")
    if "--isolated" not in args or "--headless" not in args or any(s.startswith(("--cdp-endpoint", "--extension", "--user-data-dir", "--shared-browser-context")) for s in args):
        raise WorkflowError("Manager playwright MCP requires isolated, headless sessions without a shared browser")
    env = value.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise WorkflowError("Project playwright MCP env must contain string values")
    return {"command": value["command"], "args": args, "env": env}


def terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class PreviewSession:
    START_TIMEOUT = 120
    SESSION_TIMEOUT = 3600
    RENEW_INTERVAL = 60

    def __init__(self, owner):
        self.owner = owner
        self.process = None
        self.record = None
        self.record_path = None
        self.log = None
        self.lock = threading.RLock()
        self.finished = threading.Event()

    def start(self, arguments: dict) -> dict:
        if not isinstance(arguments, dict) or set(arguments) - {"profile"} or ("profile" in arguments and not isinstance(arguments["profile"], str)):
            raise WorkflowError("start_browser_preview accepts only an approved profile name; no command overrides")
        profile = arguments.get("profile")
        with self.lock:
            workflow = self.owner.active_workflow()
            settings = browser_config(workflow.config, profile)
            if self.owner.role != "manager" or settings is None:
                raise WorkflowError("Browser preview is only enabled for Manager with verification.browser")
            if self.process is not None:
                if self.process.poll() is None:
                    if self.record.get("profile") != profile:
                        raise WorkflowError("Stop the current preview before switching profiles")
                    return self.record
                self.stop("server_exited")
                raise WorkflowError("Preview server exited; inspect its log before retrying")
            artifacts = workflow.root / ".ralph/verification" / self.owner.claim_id / "browser"
            directory = artifacts / f"session-{uuid.uuid4().hex}"
            directory.mkdir(parents=True, mode=0o700)
            data = directory / "data"
            data.mkdir(mode=0o700)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            def expand(value):
                return value.replace("{port}", str(port)).replace("{dataDir}", str(data))
            if __package__:
                from .preparation_workspace import preparation_command, preparation_snapshot
            else:
                from preparation_workspace import preparation_command, preparation_snapshot
            preparation = preparation_snapshot(workflow)
            command = preparation_command(workflow, [expand(s) for s in settings["command"]])
            if __package__:
                from .host_verification import SAFE_ENV, fingerprint
            else:
                from host_verification import SAFE_ENV, fingerprint
            cwd = Path(workflow.state["cwd"])
            self.record_path = directory / "preview.json"
            self.record = {"taskId": workflow.state["task"]["id"], "claimId": self.owner.claim_id,
                "role": self.owner.role, "profile": profile, "cwd": str(cwd), "headSha": run("git", "rev-parse", "HEAD", cwd=cwd),
                "preparationWorkspace": preparation, "executedCommand": command, "sourceFingerprint": fingerprint(cwd), "url": f"http://127.0.0.1:{port}",
                "artifactDirectory": str(artifacts), "recordPath": str(self.record_path), "dataDirectory": str(data), "logPath": str(directory / "server.log"),
                "startedAt": int(time.time() * 1000), "status": "starting"}
            atomic_json(self.record_path, self.record)
            self.log = Path(self.record["logPath"]).open("w")
            env = {k: v for k, v in os.environ.items() if k in SAFE_ENV}
            env.update({k: expand(v) for k, v in settings["env"].items()})
            try:
                self.record["preparation"] = []
                for index, setup in enumerate(settings["setupCommands"]):
                    self.owner.active_workflow()
                    setup_log = directory / f"setup-{index + 1}.log"
                    entry = {"command": preparation_command(workflow, [expand(arg) for arg in setup]), "logPath": str(setup_log), "exitCode": None}
                    self.record["preparation"].append(entry)
                    atomic_json(self.record_path, self.record)
                    with setup_log.open("w") as output:
                        self.process = subprocess.Popen(entry["command"], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                        deadline = time.monotonic() + self.START_TIMEOUT
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise WorkflowError("Preview preparation timed out; inspect setup log")
                            try:
                                entry["exitCode"] = self.process.wait(timeout=min(30, remaining))
                                break
                            except subprocess.TimeoutExpired:
                                self.owner.active_workflow()
                    atomic_json(self.record_path, self.record)
                    if entry["exitCode"] != 0:
                        raise WorkflowError("Preview preparation failed; inspect setup log")
                    terminate(self.process)
                    self.process = None
                self.owner.active_workflow()
                if fingerprint(cwd) != self.record["sourceFingerprint"] or run("git", "rev-parse", "HEAD", cwd=cwd) != self.record["headSha"]:
                    raise WorkflowError("Preview preparation changed source or HEAD")
                # Child runtimes must not alter the MCP input pipe or consume its requests.
                self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=self.log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                self.record["pid"] = self.process.pid
                atomic_json(self.record_path, self.record)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + self.START_TIMEOUT
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise WorkflowError("Preview server exited during startup; inspect server.log")
                    try:
                        with opener.open(self.record["url"] + settings["readyPath"], timeout=1) as response:
                            ready = 200 <= response.status < 400
                    except urllib.error.HTTPError as error:
                        ready = error.code in {401, 403}
                    except (urllib.error.URLError, TimeoutError):
                        ready = False
                    if ready:
                        self.owner.active_workflow()
                        self.record["status"] = "ready"
                        atomic_json(self.record_path, self.record)
                        self.finished = threading.Event()
                        threading.Thread(target=self.monitor, args=(self.finished,), daemon=True).start()
                        return dict(self.record)
                    time.sleep(0.1)
                raise WorkflowError("Preview server readiness timed out; inspect server.log")
            except BaseException:
                self.stop("startup_failed")
                raise

    def monitor(self, finished: threading.Event) -> None:
        deadline = time.monotonic() + self.SESSION_TIMEOUT
        while not finished.wait(self.RENEW_INTERVAL):
            with self.lock:
                if finished.is_set():
                    return
                try:
                    if time.monotonic() >= deadline:
                        self.stop("timeout")
                        return
                    if self.process is None or self.process.poll() is not None:
                        self.stop("server_exited")
                        return
                    self.owner.active_workflow()
                except Exception:
                    self.stop("claim_invalid")
                    return

    def stop(self, status="stopped") -> dict:
        with self.lock:
            self.finished.set()
            terminate(self.process)
            exit_code = self.process.returncode if self.process is not None else None
            self.process = None
            if self.log:
                self.log.close()
                self.log = None
            if not self.record:
                return {"status": "not_started"}
            if self.record.get("finishedAt"):
                return dict(self.record)
            self.record.update(status=status, exitCode=exit_code, finishedAt=int(time.time() * 1000))
            try:
                if __package__:
                    from .host_verification import fingerprint
                else:
                    from host_verification import fingerprint
                if __package__:
                    from .preparation_workspace import preparation_snapshot
                    from .github_workflow import Workflow
                else:
                    from preparation_workspace import preparation_snapshot
                    from github_workflow import Workflow
                workflow = Workflow(self.owner.config, self.owner.role, self.owner.principal)
                self.record["preparationUnchanged"] = self.record.get("preparationWorkspace") == preparation_snapshot(workflow)
                cwd = Path(self.record["cwd"])
                self.record["sourceUnchanged"] = self.record["sourceFingerprint"] == fingerprint(cwd) and self.record["headSha"] == run("git", "rev-parse", "HEAD", cwd=cwd)
            finally:
                atomic_json(self.record_path, self.record)
            return dict(self.record)
