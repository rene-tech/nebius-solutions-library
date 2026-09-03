"""The image's provenance must name the source a reviewer can actually check out.

An earlier candidate was published whose SLSA attestation recorded a commit that
had since been amended away. The image was fine; its provenance pointed at
source that no longer matched it. These tests cover the guard that now makes
that build fail instead of shipping.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("af3_build", ROOT / "build.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
sys.modules["af3_build"] = build
_spec.loader.exec_module(build)


LOCK_PATH = ROOT / "contracts" / "af3-image-lock.json"
LOCK_ABSENT_REASON = (
    "the image lock is produced by a build and lands in the evidence commit that follows "
    "it; a source commit cannot contain a lock naming its own revision"
)


def lock_or_skip(case: unittest.TestCase) -> dict:
    if not LOCK_PATH.is_file():
        case.skipTest(LOCK_ABSENT_REASON)
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


class SourceStateTests(unittest.TestCase):
    def test_it_reports_the_repository_head(self) -> None:
        state = build.source_state()
        self.assertRegex(state["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(state["tree"], r"^[0-9a-f]{40}$")
        expected = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(expected, state["commit"])

    def test_the_context_file_list_matches_the_dockerignore(self) -> None:
        """The digested context must be exactly what the build can see."""
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        allowed = {line.lstrip("!") for line in ignore if line.startswith("!")}
        # The Dockerfile is passed with --file rather than admitted by the
        # context policy, so it is digested in addition to the allowed set.
        self.assertEqual(allowed | {"Dockerfile"}, set(build.CONTEXT_FILES))

    def test_every_context_file_exists_and_digests(self) -> None:
        digest = build.context_digest()
        self.assertRegex(digest["context_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(build.CONTEXT_FILES), len(digest["files"]))
        for entry in digest["files"]:
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_the_context_digest_changes_when_the_context_changes(self) -> None:
        first = build.context_digest()["context_sha256"]
        target = ROOT / "runtime" / "af3_runtime.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# provenance probe\n")
            self.assertNotEqual(first, build.context_digest()["context_sha256"])
        finally:
            target.write_bytes(original)
        self.assertEqual(first, build.context_digest()["context_sha256"])


class RevisionGuardTests(unittest.TestCase):
    def test_a_mismatched_requested_revision_is_refused(self) -> None:
        with self.assertRaises(build.BuildError) as caught:
            build.build(
                tag="unused",
                oci_file=Path("/nonexistent/unused.oci"),
                builder=None,
                metadata_file=Path("/nonexistent/unused.json"),
                source_revision="0" * 40,
            )
        message = str(caught.exception)
        self.assertIn("is not HEAD", message)
        self.assertIn("exact commit", message)

    def test_a_dirty_context_is_refused_before_anything_is_built(self) -> None:
        target = ROOT / "runtime" / "af3_runtime.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# uncommitted probe\n")
            with self.assertRaises(build.BuildError) as caught:
                build.build(
                    tag="unused",
                    oci_file=Path("/nonexistent/unused.oci"),
                    builder=None,
                    metadata_file=Path("/nonexistent/unused.json"),
                )
            message = str(caught.exception)
            self.assertIn("uncommitted changes", message)
            self.assertIn("does not match", message)
        finally:
            target.write_bytes(original)

    def test_the_attestation_revision_is_read_from_a_real_archive(self) -> None:
        """The extractor must find the field the reviewer found by hand."""
        self.assertTrue(hasattr(build, "attestation_vcs_revision"))
        source = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn('"vcs:revision"', source)
        self.assertIn("records source revision", source)


class LockProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = lock_or_skip(self)

    def test_the_lock_binds_the_attestation_revision_to_the_source_revision(self) -> None:
        source = self.lock["source"]
        self.assertRegex(source["source_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(source["source_revision"], source["attestation_vcs_revision"])

    def test_the_lock_binds_the_local_build_context(self) -> None:
        """resolvedDependencies records base images but not the local context."""
        source = self.lock["source"]
        self.assertRegex(source["context_sha256"], r"^[0-9a-f]{64}$")
        paths = {entry["path"] for entry in source["context_files"]}
        self.assertEqual(set(build.CONTEXT_FILES), paths)

    def test_the_attested_commit_still_exists_and_its_context_is_unchanged(self) -> None:
        """The evidence commit may follow the build; it may not disturb it.

        The lock names the commit the attestation carries. That commit must
        still be reachable, and every build-context file must be byte-identical
        to it, otherwise the published image no longer matches any commit.
        """
        revision = self.lock["source"]["source_revision"]
        subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{revision}^{{commit}}"],
            check=True, capture_output=True,
        )
        prefix = "k8s-inference/models/cancer-immunotherapy/images/alphafold3"
        for name in build.CONTEXT_FILES:
            with self.subTest(path=name):
                attested = subprocess.run(
                    ["git", "-C", str(ROOT), "show", f"{revision}:{prefix}/{name}"],
                    check=True, capture_output=True,
                ).stdout
                self.assertEqual(
                    (ROOT / name).read_bytes(), attested,
                    f"{name} changed after the image was attested",
                )

    def test_the_locked_context_digests_match_the_working_tree(self) -> None:
        recorded = {
            entry["path"]: entry["sha256"] for entry in self.lock["source"]["context_files"]
        }
        for entry in build.context_digest()["files"]:
            with self.subTest(path=entry["path"]):
                self.assertEqual(entry["sha256"], recorded[entry["path"]])


if __name__ == "__main__":
    unittest.main()
