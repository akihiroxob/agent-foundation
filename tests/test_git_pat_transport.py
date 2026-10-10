from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph.lib.github_workflow import Workflow, WorkflowError, atomic_json, run

HELPER = Path(__file__).resolve().parents[1] / "ralph/lib/git_credential.py"
TOKEN = "synthetic-test-pat"


class GitPatTransportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        run("git", "init", cwd=self.root)
        run("git", "remote", "add", "origin", "git@github.com:org/repo.git", cwd=self.root)
        config = self.root / ".ralph/config.json"
        atomic_json(config, {"roles": {role: {"agentName": role} for role in ("worker", "reviewer", "manager")}})
        self.workflow = Workflow(config, "worker")
        self.workflow.workflow = {"repository": "org/repo"}
        self.environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "RALPH_GITHUB_TOKEN": TOKEN, "GIT_TRACE": "1", "GIT_CURL_VERBOSE": "1"}

    def tearDown(self):
        self.directory.cleanup()

    def capture_remote(self, operation="fetch"):
        calls = []

        def intercept(*args, cwd, env=None):
            if operation in args:
                calls.append((args, env))
                return ""
            return run(*args, cwd=cwd, env=env)

        with patch.dict(os.environ, self.environment, clear=True), patch("ralph.lib.github_workflow.run", side_effect=intercept):
            self.workflow.git_remote(operation, "HEAD:refs/heads/test", self.root)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def credential(self, args, environment, host="github.com", path="org/repo.git", protocol="https"):
        options = args[1:args.index("fetch") if "fetch" in args else args.index("push")]
        return subprocess.run(("git", *options, "credential", "fill"), cwd=self.root, env=environment,
            input=f"protocol={protocol}\nhost={host}\npath={path}\n\n", capture_output=True, text=True)

    def test_ssh_origin_uses_https_and_ephemeral_pat_for_fetch_and_push(self):
        original = (self.root / ".git/config").read_bytes()
        for operation in ("fetch", "push"):
            args, environment = self.capture_remote(operation)
            self.assertEqual(args[-3:], (operation, "https://github.com/org/repo.git", "HEAD:refs/heads/test"))
            self.assertNotIn(TOKEN, " ".join(args))
            self.assertNotIn("GIT_TRACE", environment)
            self.assertNotIn("GIT_CURL_VERBOSE", environment)
            self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
            filled = self.credential(args, environment)
            self.assertEqual(filled.returncode, 0, filled.stderr)
            self.assertIn("username=x-access-token", filled.stdout)
            self.assertIn(f"password={TOKEN}", filled.stdout)
        self.assertEqual(original, (self.root / ".git/config").read_bytes())

    def test_other_host_repository_or_protocol_never_receives_token(self):
        args, environment = self.capture_remote()
        for fields in ({"host": "example.test"}, {"path": "org/other.git"}, {"protocol": "http"}):
            result = self.credential(args, environment, **fields)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_configured_helpers_and_headers_are_overridden_without_saving(self):
        run("git", "config", "credential.helper", "!echo poisoned >&2; exit 1", cwd=self.root)
        run("git", "config", "credential.https://github.com.helper", "!echo poisoned >&2; exit 1", cwd=self.root)
        run("git", "config", "http.extraHeader", "Authorization: poison", cwd=self.root)
        original = (self.root / ".git/config").read_bytes()
        args, environment = self.capture_remote()
        result = self.credential(args, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("poisoned", result.stderr)
        for action in ("store", "erase"):
            result = subprocess.run((sys.executable, str(HELPER), action), env=environment,
                input=f"password={TOKEN}\n\n", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        self.assertEqual(original, (self.root / ".git/config").read_bytes())

    def test_helper_path_with_spaces_and_invalid_token(self):
        helper = self.root / "helper with spaces.py"
        helper.write_bytes(HELPER.read_bytes())
        args, environment = self.capture_remote()
        options = list(args)
        index = next(i for i, value in enumerate(options) if value.startswith("credential.helper=!"))
        options[index] = "credential.helper=!" + shlex.quote(sys.executable) + " " + shlex.quote(str(helper))
        self.assertEqual(self.credential(tuple(options), environment).returncode, 0)
        result = self.credential(tuple(options), {**environment, "RALPH_GIT_AUTH_TOKEN": TOKEN + "\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_role_specific_token_and_missing_token(self):
        self.workflow.config["github"] = {"tokenEnvs": {"worker": "WORKER_GITHUB_TOKEN"}}
        self.environment["WORKER_GITHUB_TOKEN"] = "role-specific-test-pat"
        _, environment = self.capture_remote()
        self.assertEqual(environment["RALPH_GIT_AUTH_TOKEN"], "role-specific-test-pat")
        self.environment.pop("WORKER_GITHUB_TOKEN")
        with self.assertRaisesRegex(WorkflowError, "WORKER_GITHUB_TOKEN is missing"):
            self.capture_remote()

    def test_url_rewrites_are_refused_before_remote_operation(self):
        for key, operation in (("url.ssh://git@github.com/.insteadOf", "fetch"),
                               ("url.ssh://git@github.com/.pushInsteadOf", "push")):
            run("git", "config", key, "https://github.com/", cwd=self.root)
            with self.assertRaisesRegex(WorkflowError, "URL rewrite"):
                self.capture_remote(operation)
            run("git", "config", "--unset", key, cwd=self.root)

    def test_git_failure_does_not_log_subprocess_secrets(self):
        failed = subprocess.CompletedProcess([], 128, stdout=TOKEN, stderr=TOKEN)
        with patch("ralph.lib.github_workflow.subprocess.run", return_value=failed):
            with self.assertRaises(WorkflowError) as raised:
                run("git", "fetch", cwd=self.root)
        self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
