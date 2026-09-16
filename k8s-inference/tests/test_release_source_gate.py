"""Release-source governance for mutating deploys (SAI-09).

A deployed revision must stay reproducible from a shared ref, never only from
one operator's local object store. These tests pin the wrapper behavior that
guarantees it: `apply` refuses source that is not a clean checkout anchored on
a shared release ref, exceptions are explicit and recorded, and local-only
tags do not silently satisfy the gate.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
STACK_PATH = DEPLOY_ROOT / "inference-stack"
MODULE_NAME = "inference_stack_release_gate_under_test"
LOADER = importlib.machinery.SourceFileLoader(MODULE_NAME, str(STACK_PATH))
SPEC = importlib.util.spec_from_loader(MODULE_NAME, LOADER)
assert SPEC is not None
STACK = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = STACK
LOADER.exec_module(STACK)


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class ReleaseSourceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name)
        self.origin = base / "origin.git"
        self.checkout = base / "checkout"
        self.run_root = base / "run-root"
        self.run_root.mkdir(mode=0o700)
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(self.checkout)],
            check=True,
            capture_output=True,
        )
        git(self.checkout, "config", "user.email", "gate-test@example.invalid")
        git(self.checkout, "config", "user.name", "Gate Test")
        git(self.checkout, "remote", "add", "origin", str(self.origin))
        (self.checkout / "tracked.txt").write_text("v1\n", encoding="utf-8")
        git(self.checkout, "add", "tracked.txt")
        git(self.checkout, "commit", "-m", "anchored commit")
        git(self.checkout, "push", "origin", "main")
        git(self.checkout, "fetch", "origin")

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    def add_unpushed_commit(self) -> str:
        self._commit_counter = getattr(self, "_commit_counter", 1) + 1
        (self.checkout / "tracked.txt").write_text(
            f"v{self._commit_counter}\n", encoding="utf-8"
        )
        git(self.checkout, "add", "tracked.txt")
        git(self.checkout, "commit", "-m", "unanchored commit")
        return self.head()

    def receipt(self) -> dict:
        return json.loads(
            (self.run_root / "release-source.json").read_text(encoding="utf-8")
        )

    def test_commit_on_origin_main_passes_and_records_receipt(self) -> None:
        state = STACK.enforce_release_source(
            self.run_root, self.head(), None, repository_root=self.checkout
        )
        self.assertTrue(state["anchored"])
        self.assertIn("refs/remotes/origin/main", state["anchor_refs"])
        receipt = self.receipt()
        self.assertEqual(receipt["source_commit"], self.head())
        self.assertTrue(receipt["worktree_clean"])
        self.assertIsNone(receipt["override_reason"])

    def test_unanchored_commit_is_refused(self) -> None:
        commit = self.add_unpushed_commit()
        with self.assertRaisesRegex(STACK.DeploymentError, "release-source gate"):
            STACK.enforce_release_source(
                self.run_root, commit, None, repository_root=self.checkout
            )
        self.assertFalse(self.receipt()["anchored"])

    def test_dirty_tracked_worktree_is_refused_even_when_anchored(self) -> None:
        (self.checkout / "tracked.txt").write_text("drift\n", encoding="utf-8")
        with self.assertRaisesRegex(STACK.DeploymentError, "uncommitted or untracked"):
            STACK.enforce_release_source(
                self.run_root, self.head(), None, repository_root=self.checkout
            )

    def test_untracked_files_block_the_gate(self) -> None:
        # Untracked files are build inputs: Terraform, Helm, and image builds
        # read the working tree, so they must fail the gate like tracked drift.
        (self.checkout / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        with self.assertRaisesRegex(STACK.DeploymentError, "untracked"):
            STACK.enforce_release_source(
                self.run_root, self.head(), None, repository_root=self.checkout
            )
        self.assertIn("scratch.txt", self.receipt()["dirty_paths"])

    def test_pushed_release_branch_anchors_the_commit(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "push", "origin", "HEAD:refs/heads/release/gate-test")
        git(self.checkout, "fetch", "origin")
        state = STACK.enforce_release_source(
            self.run_root, commit, None, repository_root=self.checkout
        )
        self.assertIn("refs/remotes/origin/release/gate-test", state["anchor_refs"])

    def test_pushed_deploy_tag_anchors_the_commit(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "tag", "deploy/gate-test", commit)
        git(self.checkout, "push", "origin", "refs/tags/deploy/gate-test")
        state = STACK.enforce_release_source(
            self.run_root, commit, None, repository_root=self.checkout
        )
        self.assertIn("refs/tags/deploy/gate-test", state["anchor_refs"])

    def test_local_only_tag_without_bundle_does_not_satisfy_the_gate(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "tag", "deploy/local-only", commit)
        with self.assertRaisesRegex(STACK.DeploymentError, "local tags"):
            STACK.enforce_release_source(
                self.run_root, commit, None, repository_root=self.checkout
            )
        self.assertEqual(
            self.receipt()["unanchored_local_tags"], ["refs/tags/deploy/local-only"]
        )

    def test_bundle_anchored_local_tag_satisfies_the_gate(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "tag", "-a", "-m", "anchor", "deploy/bundled", commit)
        receipt = STACK.create_release_anchor(
            self.run_root, "deploy/bundled", repository_root=self.checkout
        )
        self.assertTrue(receipt["restore_tested"])
        bundle = Path(receipt["bundle_path"])
        self.assertTrue(bundle.is_file())
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        state = STACK.enforce_release_source(
            self.run_root, commit, None, repository_root=self.checkout
        )
        self.assertEqual(state["bundle_verified_tags"], ["refs/tags/deploy/bundled"])
        self.assertEqual(state["anchor_refs"], ["refs/tags/deploy/bundled"])

    def test_tampered_bundle_no_longer_anchors_the_tag(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "tag", "-a", "-m", "anchor", "deploy/tampered", commit)
        receipt = STACK.create_release_anchor(
            self.run_root, "deploy/tampered", repository_root=self.checkout
        )
        bundle = Path(receipt["bundle_path"])
        with bundle.open("r+b") as handle:
            handle.seek(-1, 2)
            handle.write(b"\x00")
        with self.assertRaisesRegex(STACK.DeploymentError, "release-source gate"):
            STACK.enforce_release_source(
                self.run_root, commit, None, repository_root=self.checkout
            )

    def test_moved_tag_invalidates_the_recorded_anchor(self) -> None:
        commit = self.add_unpushed_commit()
        git(self.checkout, "tag", "-a", "-m", "anchor", "deploy/moved", commit)
        STACK.create_release_anchor(
            self.run_root, "deploy/moved", repository_root=self.checkout
        )
        moved = self.add_unpushed_commit()
        git(self.checkout, "tag", "-f", "-a", "-m", "moved", "deploy/moved", moved)
        with self.assertRaisesRegex(STACK.DeploymentError, "release-source gate"):
            STACK.enforce_release_source(
                self.run_root, moved, None, repository_root=self.checkout
            )

    def test_anchor_refuses_tags_outside_release_namespaces(self) -> None:
        git(self.checkout, "tag", "v1.0", self.head())
        with self.assertRaisesRegex(STACK.DeploymentError, "refs/tags/release/"):
            STACK.create_release_anchor(
                self.run_root, "v1.0", repository_root=self.checkout
            )

    def test_override_records_reason_instead_of_weakening_silently(self) -> None:
        commit = self.add_unpushed_commit()
        state = STACK.enforce_release_source(
            self.run_root,
            commit,
            "governed exception: incident rollforward",
            repository_root=self.checkout,
        )
        self.assertFalse(state["anchored"])
        self.assertEqual(
            self.receipt()["override_reason"],
            "governed exception: incident rollforward",
        )


class ApplyCommandGateWiringTest(unittest.TestCase):
    """`apply` must run the release-source gate before mutating anything."""

    def test_apply_enforces_release_source_before_apply_stack(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            var_file = Path(temporary) / "terraform.tfvars"
            var_file.write_text("# test\n", encoding="utf-8")
            run_root = Path(temporary) / "run"
            contract = {"name": "gate-test", "profiles": {}}
            with (
                mock.patch.object(STACK, "require_terraform_version"),
                mock.patch.object(
                    STACK,
                    "validate_configuration",
                    return_value=(contract, {}),
                ),
                mock.patch.object(
                    STACK, "source_commit", return_value="a" * 40
                ),
                mock.patch.object(STACK, "require_deployable_images"),
                mock.patch.object(
                    STACK,
                    "enforce_release_source",
                    side_effect=lambda *a, **k: calls.append("gate"),
                ),
                mock.patch.object(
                    STACK,
                    "apply_stack",
                    side_effect=lambda *a, **k: calls.append("apply"),
                ),
            ):
                STACK.main(
                    [
                        "apply",
                        "--var-file",
                        str(var_file),
                        "--run-root",
                        str(run_root),
                    ]
                )
        self.assertEqual(calls, ["gate", "apply"])

    def test_release_gate_command_prints_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            var_file = Path(temporary) / "terraform.tfvars"
            var_file.write_text("# test\n", encoding="utf-8")
            run_root = Path(temporary) / "run"
            contract = {"name": "gate-test", "profiles": {}}
            state = {"anchored": True, "anchor_refs": ["refs/remotes/origin/main"]}
            with (
                mock.patch.object(STACK, "require_terraform_version"),
                mock.patch.object(
                    STACK,
                    "validate_configuration",
                    return_value=(contract, {}),
                ),
                mock.patch.object(
                    STACK, "source_commit", return_value="a" * 40
                ),
                mock.patch.object(
                    STACK, "enforce_release_source", return_value=state
                ) as gate,
                mock.patch.object(STACK, "apply_stack") as apply_stack,
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    STACK.main(
                        [
                            "release-gate",
                            "--var-file",
                            str(var_file),
                            "--run-root",
                            str(run_root),
                        ]
                    )
        gate.assert_called_once()
        apply_stack.assert_not_called()
        self.assertTrue(json.loads(stdout.getvalue())["anchored"])


if __name__ == "__main__":
    unittest.main()
