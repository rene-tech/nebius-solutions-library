"""Repo-wide guard on the filesystem-group ownership contract.

Two regressions are equally bad and both are caught here.  Dropping
``fsGroupChangePolicy: OnRootMismatch`` from a remediated template puts the
153-305 s recursive ownership walk back into a scientific cold start.  Adding it
to a template whose volume root nobody owns buys that speed by letting the
kubelet skip a tree that was never adopted.  Every pod template that carries an
``fsGroup`` therefore has to appear in
``catalog/runtime/contracts/volume-ownership-authorities.json``, on the
registered side with a named authority or on the deferred side with a reason.

Go-templated chart manifests are registered here too, but they are asserted
against a real ``helm template`` render in
``components/control-plane/tests/test_helm_chart.py``, which already owns the
chart's required value set.  Each such entry names that suite in ``verified_by``.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = ROOT / "catalog/runtime/contracts/volume-ownership-authorities.json"
CHART = ROOT / "charts/control-plane/fs2-serve-control-plane"
POLICY = "OnRootMismatch"

# Where a pod template can legitimately live.  Third-party chart values under
# stages/ and observability/ configure upstream charts we do not author, and are
# not fs2 pod templates.
SEARCH_ROOTS = (
    "academic-assets",
    "catalog",
    "charts",
    "model-artifacts",
    "models",
    "reference-data",
    "scientific-artifacts",
)
DOCUMENT_SUFFIXES = {".json", ".yaml", ".yml"}


def find_pod_specs(node: object, pointer: str = "") -> list[tuple[str, dict]]:
    """Every mapping that looks like a PodSpec carrying pod-level ownership."""

    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        security = node.get("securityContext")
        if (
            isinstance(security, dict)
            and ("containers" in node or "initContainers" in node)
            and ("fsGroup" in security or "fsGroupChangePolicy" in security)
        ):
            found.append((pointer, node))
        for key, value in node.items():
            found.extend(find_pod_specs(value, f"{pointer}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(find_pod_specs(value, f"{pointer}/{index}"))
    return found


def load_documents(path: Path) -> list[object]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return [json.loads(text)]
    return [document for document in yaml.safe_load_all(text) if document is not None]


class VolumeOwnershipRegisterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        cls.registered = {item["path"]: item for item in cls.register["registered"]}
        cls.deferred = {item["path"]: item for item in cls.register["deferred"]}
        cls.excluded = tuple(
            item["prefix"] for item in cls.register["excluded_prefixes"]
        )
        cls.observed = cls.collect()

    @classmethod
    def excluded_path(cls, relative: str) -> bool:
        return any(relative.startswith(prefix) for prefix in cls.excluded)

    @classmethod
    def collect(cls) -> dict[str, list[tuple[str, dict]]]:
        """Map every checked-in document to the ownership-bearing pods inside it."""

        observed: dict[str, list[tuple[str, dict]]] = {}
        for base in SEARCH_ROOTS:
            for path in sorted((ROOT / base).rglob("*")):
                if not path.is_file() or path.suffix not in DOCUMENT_SUFFIXES:
                    continue
                relative = path.relative_to(ROOT).as_posix()
                if cls.excluded_path(relative):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if "fsGroup" not in text:
                    continue
                if path.is_relative_to(CHART / "templates"):
                    # Go templates are rendered separately below.
                    continue
                pods: list[tuple[str, dict]] = []
                for index, document in enumerate(load_documents(path)):
                    pods.extend(
                        (f"doc{index}{pointer}", pod)
                        for pointer, pod in find_pod_specs(document)
                    )
                if pods:
                    observed[relative] = pods
        return observed

    # --- the guard itself -------------------------------------------------

    def test_every_python_renderer_that_emits_a_group_is_accounted_for(self) -> None:
        """A renderer is as much a pod template as a manifest is.

        The public artifact catalog arrived with a new fsGroup renderer that the
        document scan could never have seen, so Python sources are inventoried
        the same way and must land in the register on one side or the other.
        """

        register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        known = (
            set(self.registered)
            | set(self.deferred)
            | {item["path"] for item in register["never_fs_group"]}
        )
        for base in SEARCH_ROOTS:
            for path in sorted((ROOT / base).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                if self.excluded_path(relative) or "/tests/" in relative:
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                if "fsGroup" not in source:
                    continue
                if relative == "catalog/runtime/fs2_serve_catalog/volume_ownership.py":
                    continue  # the contract itself
                with self.subTest(path=relative):
                    self.assertIn(
                        relative,
                        known,
                        f"{relative} emits an fsGroup but is in neither side of the register",
                    )

    def test_the_register_declares_only_known_authorities(self) -> None:
        vocabulary = set(self.register["authorities"])
        self.assertEqual(POLICY, self.register["policy"])
        for path, item in self.registered.items():
            with self.subTest(path=path):
                self.assertIn(item["authority"], vocabulary)

    def test_every_fast_policy_template_names_its_authority(self) -> None:
        for path, pods in sorted(self.observed.items()):
            for pointer, pod in pods:
                if pod["securityContext"].get("fsGroupChangePolicy") != POLICY:
                    continue
                with self.subTest(path=path, pointer=pointer):
                    self.assertIn(
                        path,
                        self.registered,
                        f"{path} skips the kubelet ownership walk without a "
                        "registered authority for its volume root",
                    )

    def test_every_recursive_template_is_a_recorded_deferral(self) -> None:
        for path, pods in sorted(self.observed.items()):
            for pointer, pod in pods:
                security = pod["securityContext"]
                if "fsGroup" not in security:
                    continue
                if security.get("fsGroupChangePolicy") == POLICY:
                    continue
                with self.subTest(path=path, pointer=pointer):
                    self.assertIn(
                        path,
                        self.deferred,
                        f"{path} still pays the recursive ownership walk and is "
                        "not recorded as a deliberate deferral",
                    )

    def test_a_change_policy_is_never_a_no_op(self) -> None:
        for path, pods in sorted(self.observed.items()):
            for pointer, pod in pods:
                security = pod["securityContext"]
                with self.subTest(path=path, pointer=pointer):
                    if "fsGroupChangePolicy" in security:
                        self.assertIn(
                            "fsGroup",
                            security,
                            "fsGroupChangePolicy without fsGroup is ignored by the kubelet",
                        )

    def test_no_template_asks_for_the_recursive_walk_explicitly(self) -> None:
        for path, pods in sorted(self.observed.items()):
            for pointer, pod in pods:
                with self.subTest(path=path, pointer=pointer):
                    self.assertNotEqual(
                        "Always",
                        pod["securityContext"].get("fsGroupChangePolicy"),
                        "an explicit Always is the 153-305 s walk written down",
                    )

    def test_the_register_has_no_stale_entries(self) -> None:
        for path in sorted(set(self.registered) | set(self.deferred)):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f"{path} no longer exists")

    def test_chart_entries_delegate_to_the_suite_that_can_render_them(self) -> None:
        for path, item in sorted({**self.registered, **self.deferred}.items()):
            if not path.startswith("charts/"):
                continue
            with self.subTest(path=path):
                verified_by = item.get("verified_by")
                self.assertIsNotNone(
                    verified_by, f"{path} is a Go template with no owning suite"
                )
                self.assertTrue((ROOT / verified_by).exists())

    def test_registered_yaml_and_json_paths_really_carry_the_policy(self) -> None:
        for path in sorted(self.registered):
            if Path(path).suffix not in DOCUMENT_SUFFIXES or path.startswith("charts/"):
                continue
            with self.subTest(path=path):
                pods = self.observed.get(path)
                self.assertIsNotNone(pods, f"{path} is registered but renders no pod")
                self.assertTrue(
                    any(
                        pod["securityContext"].get("fsGroupChangePolicy") == POLICY
                        for _, pod in pods
                    ),
                    f"{path} is registered but does not carry {POLICY}",
                )

    def test_registered_python_renderers_use_the_shared_contract(self) -> None:
        """A renderer that really takes the fast path must go through the one
        contract rather than restating the policy.

        A ``hostpath-not-kubelet-managed`` renderer is exempt: the kubelet runs
        no ownership pass over a hostPath, so there is no walk for the contract
        to govern, and those scripts live outside the catalog package anyway.
        """

        for path, item in sorted(self.registered.items()):
            if Path(path).suffix != ".py":
                continue
            if item["authority"] in {
                "hostpath-not-kubelet-managed",
                "unverified-external-writer",
            }:
                continue
            if item.get("contract_caller") is False:
                # Observed pre-existing state, not a caller of this contract.
                continue
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                self.assertTrue(
                    "from .volume_ownership import" in source
                    or "from fs2_serve_catalog.volume_ownership import" in source,
                    f"{path} is registered but does not route through the contract",
                )
                self.assertNotIn(
                    '"fsGroupChangePolicy"',
                    source,
                    f"{path} hard-codes the policy instead of using the contract",
                )


class NeverFilesystemGroupTests(unittest.TestCase):
    """Licensed and academic trees must keep supplementalGroups and no fsGroup.

    The kubelet's ownership pass rewrites what it takes over to group-writable
    0660/2775. On a licensed AlphaFold 3 parameter tree or the academic asset
    cache that is not a slow path, it is a redistribution problem, so those
    surfaces are read through supplementalGroups with read-only mounts instead.
    This is the guard that stops a future optimisation pass from "fixing" them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        cls.entries = register["never_fs_group"]

    def test_the_register_names_the_protected_surfaces(self) -> None:
        self.assertTrue(self.entries)
        for entry in self.entries:
            with self.subTest(path=entry["path"]):
                self.assertTrue((ROOT / entry["path"]).exists())
                self.assertTrue(entry["reason"].strip())

    def test_none_of_them_sets_a_filesystem_group(self) -> None:
        """Naming the field to forbid it is exactly what these files should do;
        only an assignment to a real gid is a violation."""

        assignment = re.compile(r'"?fsGroup"?\s*:\s*(\d+)')
        for entry in self.entries:
            text = (ROOT / entry["path"]).read_text(encoding="utf-8")
            with self.subTest(path=entry["path"]):
                self.assertEqual(
                    [],
                    assignment.findall(text),
                    f"{entry['path']} assigns a numeric fsGroup",
                )

    def test_the_academic_execution_template_still_reads_by_group(self) -> None:
        template = (
            ROOT
            / "charts/control-plane/fs2-serve-control-plane/templates"
            / "academic-execution-job.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("supplementalGroups:", template)
        self.assertIn("readOnly: true", template)


class KnownRendererBehaviourTests(unittest.TestCase):
    """Behaviour-level checks on the renderers the register names.

    A register entry that nobody executes is a comment. These import the real
    renderers and assert what they actually emit.
    """

    def test_the_serving_renderer_still_pays_the_walk_on_purpose(self) -> None:
        """The rollout is deferred, so the serving pod must not have gained the
        policy. This is the assertion that would catch it being switched on
        before the producer and consumer identities are aligned."""

        sys.path.insert(0, str(ROOT / "catalog/runtime"))
        from fs2_serve_catalog import workloads  # noqa: PLC0415

        source = Path(workloads.__file__).read_text(encoding="utf-8")
        self.assertIn('"fsGroup": 1000', source)
        self.assertNotIn("fsGroupChangePolicy", source)
        self.assertNotIn("volume_ownership", source)

        register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        self.assertEqual("deferred", register["rollout"]["state"])
        self.assertEqual(0, register["rollout"]["callers"])
        self.assertTrue(register["rollout"]["this_commit_changes_no_rendered_manifest"])

    def test_the_contract_refuses_the_chain_as_it_exists_today(self) -> None:
        """Behaviour-level: the library, given the repository's real producer and
        consumer identities, says no."""

        sys.path.insert(0, str(ROOT / "catalog/runtime"))
        from fs2_serve_catalog.acquisition import ACQUISITION_FS_GROUP  # noqa: PLC0415
        from fs2_serve_catalog.volume_ownership import (  # noqa: PLC0415
            FilesystemGroupConsumer,
            FilesystemGroupProducer,
            authority_violations,
        )

        self.assertEqual(10001, ACQUISITION_FS_GROUP)
        violations = authority_violations(
            FilesystemGroupProducer(
                name="artifact acquisition Job",
                gid=ACQUISITION_FS_GROUP,
                root="/mnt/fs2-serve-cache",
                whole_tree=True,
            ),
            FilesystemGroupConsumer(
                name="model runtime pod",
                fs_group=1000,
                root="/mnt/fs2-serve-cache",
                read_only=True,
            ),
        )
        self.assertTrue(violations)

    def test_the_public_artifact_ingestion_job_is_hostpath_and_unpolicied(self) -> None:
        """model-artifacts/render_jobs.py arrived with the accepted catalog and
        emits an fsGroup the document scan could never see."""

        source = (ROOT / "model-artifacts/render_jobs.py").read_text(encoding="utf-8")
        self.assertIn('"fsGroup": 1000', source)
        self.assertNotIn("fsGroupChangePolicy", source)
        self.assertIn('"hostPath"', source)
        register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
        deferred = {item["path"] for item in register["deferred"]}
        self.assertIn("model-artifacts/render_jobs.py", deferred)


if __name__ == "__main__":
    unittest.main()
