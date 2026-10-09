import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SOURCE = Path(__file__).with_name("single_main.py")
spec = importlib.util.spec_from_file_location("single_main", SOURCE)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scientific-main-test-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        self.repo.mkdir()
        workflow.git(self.repo, "init", "-b", "main")
        workflow.git(self.repo, "config", "user.name", "Workflow Test")
        workflow.git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / ".scientific-ai-workflow.json").write_text(json.dumps({"personal_repository": "rene-tech/test"}))
        (self.repo / "file.txt").write_text("initial\n")
        hooks = self.repo / ".githooks"
        scripts = self.repo / "scripts"
        hooks.mkdir()
        scripts.mkdir()
        shutil.copy2(SOURCE, scripts / "single_main.py")
        for name in ["pre-push", "reference-transaction"]:
            hook = hooks / name
            hook.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport runpy,sys\nsys.argv.insert(1,"hook-'+name+'")\nrunpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts"/"single_main.py"),run_name="__main__")\n')
            hook.chmod(0o755)
        workflow.git(self.repo, "add", ".")
        workflow.git(self.repo, "commit", "-m", "initial")
        workflow.git(self.repo, "config", "scientificai.singlemain", "true")
        workflow.git(self.repo, "config", "core.hooksPath", str(hooks))
        self.initial = workflow.value(self.repo, "rev-parse", "main")

    def tearDown(self):
        self.temp.cleanup()

    def task(self):
        task = self.base / "task"
        workflow.git(self.repo, "worktree", "add", "--detach", str(task), "main")
        (task / "file.txt").write_text("task\n")
        workflow.git(task, "add", "file.txt")
        workflow.git(task, "commit", "-m", "task")
        sha = workflow.value(task, "rev-parse", "HEAD")
        evidence = self.base / "checks.json"
        evidence.write_text(json.dumps({"commit": sha, "checks": [{"command": "actual-test-command", "exit_code": 0}]}))
        return task, evidence, sha

    def test_temporary_branches_and_remote_tracking_refs_are_allowed(self):
        result = workflow.git(self.repo, "branch", "agent/leak", check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("refs/heads/agent/leak", workflow.value(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads"))
        workflow.git(self.repo, "update-ref", "refs/remotes/test/archive", self.initial)

    def test_integration_retires_the_completed_temporary_branch(self):
        task, evidence, sha = self.task()
        workflow.git(task, "checkout", "-b", "task/completed")
        self.assertEqual(workflow.integrate(self.repo, task, evidence, False), sha)
        self.assertNotEqual(workflow.git(task, "symbolic-ref", "-q", "HEAD", check=False).returncode, 0)
        self.assertEqual(workflow.value(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")
        self.assertEqual((task / "file.txt").read_text(), "task\n")
        self.assertTrue((workflow.common(self.repo) / "scientificai-closeout" / f"{sha}.json").exists())

    def test_unmerged_temporary_branch_is_not_deleted(self):
        task, evidence, sha = self.task()
        workflow.git(task, "checkout", "-b", "task/unfinished")
        with self.assertRaisesRegex(ValueError, "Unmerged"):
            workflow.retire_task_branch(self.repo, task, "refs/heads/task/unfinished", sha, False)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "task/unfinished"), sha)

    def test_local_main_history_rewrite_is_rejected(self):
        task, evidence, sha = self.task()
        workflow.integrate(self.repo, task, evidence, False)
        result = workflow.git(self.repo, "update-ref", "refs/heads/main", self.initial, sha, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "main"), sha)

    def test_fast_forward_updates_main_files_and_preserves_detached_task(self):
        task, evidence, sha = self.task()
        self.assertEqual(workflow.integrate(self.repo, task, evidence, False), sha)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "main"), sha)
        self.assertEqual((self.repo / "file.txt").read_text(), "task\n")
        self.assertNotEqual(workflow.git(task, "symbolic-ref", "-q", "HEAD", check=False).returncode, 0)
        self.assertEqual(workflow.value(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/main")

    def test_stale_main_cannot_be_overwritten(self):
        task, evidence, sha = self.task()
        (self.repo / "sibling.txt").write_text("sibling\n")
        workflow.git(self.repo, "add", "sibling.txt")
        workflow.git(self.repo, "commit", "-m", "sibling")
        current = workflow.value(self.repo, "rev-parse", "main")
        with self.assertRaisesRegex(ValueError, "Main moved"):
            workflow.integrate(self.repo, task, evidence, False)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "main"), current)
        self.assertEqual((self.repo / "file.txt").read_text(), "initial\n")

    def test_dirty_task_and_stale_evidence_cannot_modify_main(self):
        task, evidence, sha = self.task()
        (task / "keep.txt").write_text("dirty\n")
        with self.assertRaisesRegex(ValueError, "dirty files"):
            workflow.integrate(self.repo, task, evidence, False)
        (task / "keep.txt").unlink()
        evidence.write_text(json.dumps({"commit": self.initial, "checks": [{"command": "test", "exit_code": 0}]}))
        with self.assertRaisesRegex(ValueError, "exact commit"):
            workflow.integrate(self.repo, task, evidence, False)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "main"), self.initial)

    def test_push_guards_allow_temporary_branches_and_reject_upstream_and_main_deletion(self):
        task, evidence, sha = self.task()
        workflow.integrate(self.repo, task, evidence, False)
        url = "https://github.com/rene-tech/test.git"
        workflow.pre_push(self.repo, url, [f"refs/heads/main {sha} refs/heads/main {self.initial}"])
        workflow.pre_push(self.repo, url, [f"HEAD {sha} refs/heads/agent/temporary {'0'*40}"])
        workflow.pre_push(self.repo, url, [f"(delete) {'0'*40} refs/heads/agent/temporary {sha}"])
        for target_url, row in [
            ("https://github.com/nebius/test.git", f"refs/heads/main {sha} refs/heads/main {self.initial}"),
            (url, f"(delete) {'0'*40} refs/heads/main {sha}"),
            (url, f"HEAD {self.initial} refs/heads/main {sha}"),
        ]:
            with self.assertRaises(ValueError):
                workflow.pre_push(self.repo, target_url, [row])

    def test_a_second_integrator_cannot_acquire_the_main_lock(self):
        task, evidence, sha = self.task()
        with (workflow.common(self.repo) / "scientificai-main.lock").open("a+") as lock:
            workflow.fcntl.flock(lock, workflow.fcntl.LOCK_EX | workflow.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                workflow.integrate(self.repo, task, evidence, False)
        self.assertEqual(workflow.value(self.repo, "rev-parse", "main"), self.initial)


if __name__ == "__main__":
    unittest.main()
