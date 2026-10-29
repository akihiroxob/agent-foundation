from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import sys
import tempfile
import unittest

from ralph.lib.github_workflow import WorkflowError, atomic_json, run
from ralph.lib.preparation_workspace import (assert_preparation_verified, ensure_preparation,
    preparation_command, preparation_files, preparation_snapshot)


class PreparationWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=self.root)
        run("git", "config", "user.email", "test@example.test", cwd=self.root)
        (self.root / "app.txt").write_text("product")
        (self.root / "scripts").mkdir()
        (self.root / "scripts/prepare.py").write_text("print('pinned preparation')\n")
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "pinned product", cwd=self.root)
        self.cwd = self.root / ".ralph/worktrees/reviewer/reviewer"
        run("git", "worktree", "add", "--detach", str(self.cwd), "HEAD", cwd=self.root)
        self.workflow = SimpleNamespace(root=self.root, role="reviewer",
            config={"verification": {"preparationFiles": ["scripts/prepare.py"]}},
            state={"cwd": str(self.cwd), "claimId": "claim123"})

    def test_uses_pinned_revision_and_keeps_product_and_fixed_checkout_unchanged(self):
        (self.root / "scripts/prepare.py").write_text("stale root checkout")
        workspace = ensure_preparation(self.workflow)
        copy = Path(workspace["directory"]) / "scripts/prepare.py"
        self.assertIn("pinned preparation", copy.read_text())
        copy.write_text("print('repaired preparation')\n")
        command = [sys.executable, str(self.root / "scripts/prepare.py")]
        mapped = preparation_command(self.workflow, command)
        self.assertEqual(str(copy), mapped[1])
        self.assertEqual("repaired preparation", subprocess.check_output(mapped, cwd=self.cwd, text=True).strip())
        self.assertEqual("stale root checkout", (self.root / "scripts/prepare.py").read_text())
        self.assertEqual("print('pinned preparation')\n", (self.cwd / "scripts/prepare.py").read_text())
        self.assertEqual("", run("git", "status", "--porcelain", cwd=self.cwd))
        snapshot = preparation_snapshot(self.workflow)
        self.assertEqual(["scripts/prepare.py"], snapshot["changedFiles"])
        self.assertIn("repaired preparation", Path(snapshot["patchPath"]).read_text())
        self.assertEqual(copy.read_bytes(), (Path(snapshot["snapshotDirectory"]) / "scripts/prepare.py").read_bytes())
        self.assertEqual(workspace, ensure_preparation(self.workflow))

    def test_newer_preparation_revision_does_not_change_pinned_product_sha(self):
        pinned = run("git", "rev-parse", "HEAD", cwd=self.cwd)
        (self.root / "scripts/prepare.py").write_text("print('newer compatible preparation')\n")
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "update preparation only", cwd=self.root)
        revision = run("git", "rev-parse", "HEAD", cwd=self.root)
        self.workflow.config["verification"]["preparationRevision"] = revision
        workspace = ensure_preparation(self.workflow)
        self.assertEqual(pinned, workspace["headSha"])
        self.assertEqual(revision, workspace["sourceSha"])
        self.assertIn("newer compatible", (Path(workspace["directory"]) / "scripts/prepare.py").read_text())
        self.assertEqual(pinned, run("git", "rev-parse", "HEAD", cwd=self.cwd))

    def test_permission_is_explicit_role_scoped_and_exact_path_only(self):
        for bad in ("scripts", ["../app.txt"], ["/tmp/prepare.py"], [".git/config"], [".ralph/config.json"], ["scripts/../app.txt"], ["scripts/prepare.py/"]):
            with self.assertRaises(WorkflowError): preparation_files({"verification": {"preparationFiles": bad}})
        self.workflow.role = "worker"
        self.assertIsNone(ensure_preparation(self.workflow))
        self.workflow.role = "reviewer"
        self.workflow.config["verification"]["preparationFiles"] = ["missing.py"]
        with self.assertRaisesRegex(WorkflowError, "pinned revision"):
            ensure_preparation(self.workflow)
        self.assertFalse((self.root / ".ralph/verification/claim123/preparation").exists())

    def test_unlisted_files_symlinks_and_changed_policy_are_refused(self):
        workspace = ensure_preparation(self.workflow)
        directory = Path(workspace["directory"])
        (directory / "app.txt").write_text("unexpected product substitute")
        with self.assertRaisesRegex(WorkflowError, "Unexpected"):
            preparation_snapshot(self.workflow)
        (directory / "app.txt").unlink()
        (directory / "scripts/prepare.py").unlink()
        (directory / "scripts/prepare.py").symlink_to(self.cwd / "app.txt")
        with self.assertRaisesRegex(WorkflowError, "redirected"):
            preparation_snapshot(self.workflow)
        self.workflow.config["verification"]["preparationFiles"] = ["app.txt"]
        with self.assertRaisesRegex(WorkflowError, "boundary changed"):
            ensure_preparation(self.workflow)

    def test_repair_requires_current_claim_sha_and_fingerprint_verification(self):
        workspace = ensure_preparation(self.workflow)
        copy = Path(workspace["directory"]) / "scripts/prepare.py"
        copy.write_text("print('new preparation')\n")
        current = preparation_snapshot(self.workflow)
        with self.assertRaisesRegex(WorkflowError, "fresh successful"):
            assert_preparation_verified(self.workflow)
        path = self.root / ".ralph/verification/claim123/host-result.json"
        record = {"claimId": "claim123", "headSha": current["headSha"], "sourceUnchanged": True,
            "preparationUnchanged": True, "preparationWorkspace": current, "status": "passed"}
        atomic_json(path, record)
        self.assertEqual(current, assert_preparation_verified(self.workflow))
        copy.write_text("print('changed after verification')\n")
        with self.assertRaisesRegex(WorkflowError, "fresh successful"):
            assert_preparation_verified(self.workflow)
        copy.write_text("print('new preparation')\n")
        for key, value in (("claimId", "another"), ("headSha", "0" * 40), ("sourceUnchanged", False),
                           ("preparationUnchanged", False), ("status", "failed")):
            atomic_json(path, {**record, key: value})
            with self.assertRaises(WorkflowError): assert_preparation_verified(self.workflow)
