"""Contract tests for the single filesystem-group ownership renderer.

These prove the defect and the fix, not just the happy path: every rejection
below is a shape that really did cost 153-305 s of scientific cold start, or
would trade that saving against correctness by letting the kubelet skip a tree
no declared producer owns.
"""

from __future__ import annotations

import unittest

import re
from pathlib import Path

from fs2_serve_catalog.volume_ownership import (
    FS_GROUP_CHANGE_POLICY,
    FilesystemGroupConsumer,
    FilesystemGroupProducer,
    assert_authority,
    authority_violations,
    RECURSIVE_FS_GROUP_CHANGE_POLICY,
    VolumeOwnershipError,
    apply_filesystem_group,
    assert_pod_volume_ownership,
    filesystem_group_security_context,
    persistent_mounts,
    validate_pod_volume_ownership,
)


def pod_with_workspace(*, fs_group: int | None = 10001, policy: str | None = None) -> dict:
    security: dict = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001}
    if fs_group is not None:
        security["fsGroup"] = fs_group
    if policy is not None:
        security["fsGroupChangePolicy"] = policy
    return {
        "restartPolicy": "Never",
        "securityContext": security,
        "containers": [
            {
                "name": "batch",
                "volumeMounts": [
                    {"name": "request", "mountPath": "/var/run/fs2", "readOnly": True},
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "request", "configMap": {"name": "fs2-run"}},
            {"name": "workspace", "persistentVolumeClaim": {"claimName": "fs2-cache"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
        ],
    }


class SecurityContextTests(unittest.TestCase):
    def test_the_contract_always_emits_the_non_recursive_policy(self) -> None:
        security = filesystem_group_security_context(
            fs_group=10001, run_as_user=10001, run_as_group=10001
        )
        self.assertEqual(FS_GROUP_CHANGE_POLICY, security["fsGroupChangePolicy"])
        self.assertEqual(10001, security["fsGroup"])
        self.assertEqual({"type": "RuntimeDefault"}, security["seccompProfile"])

    def test_a_root_or_absent_filesystem_group_is_refused(self) -> None:
        for value in (0, -1, True, "10001", None):
            with self.subTest(fs_group=value):
                with self.assertRaises(VolumeOwnershipError):
                    filesystem_group_security_context(fs_group=value)  # type: ignore[arg-type]

    def test_callers_cannot_smuggle_their_own_ownership_fields(self) -> None:
        for field in ("fsGroup", "fsGroupChangePolicy"):
            with self.subTest(field=field):
                with self.assertRaises(VolumeOwnershipError):
                    filesystem_group_security_context(
                        fs_group=10001, extra={field: "Always"}
                    )

    def test_apply_replaces_a_recursive_pod_security_context(self) -> None:
        pod = pod_with_workspace(policy=RECURSIVE_FS_GROUP_CHANGE_POLICY)
        apply_filesystem_group(pod, fs_group=10001, run_as_user=10001, run_as_group=10001)
        self.assertEqual(
            FS_GROUP_CHANGE_POLICY, pod["securityContext"]["fsGroupChangePolicy"]
        )


class ValidatorTests(unittest.TestCase):
    def test_the_recorded_defect_is_rejected(self) -> None:
        violations = validate_pod_volume_ownership(
            pod_with_workspace(), owned_roots=("/workspace",)
        )
        self.assertEqual(1, len(violations), violations)
        self.assertIn("recursive volume ownership walk", violations[0])

    def test_an_explicit_always_policy_is_rejected_too(self) -> None:
        violations = validate_pod_volume_ownership(
            pod_with_workspace(policy=RECURSIVE_FS_GROUP_CHANGE_POLICY),
            owned_roots=("/workspace",),
        )
        self.assertTrue(any("recursive volume ownership walk" in item for item in violations))

    def test_a_policy_without_a_group_is_rejected_as_a_no_op(self) -> None:
        violations = validate_pod_volume_ownership(
            pod_with_workspace(fs_group=None, policy=FS_GROUP_CHANGE_POLICY)
        )
        self.assertEqual(
            ["fsGroupChangePolicy is set without an fsGroup, so the kubelet ignores it"],
            violations,
        )

    def test_a_writable_claim_without_a_declared_authority_is_rejected(self) -> None:
        violations = validate_pod_volume_ownership(
            pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        )
        self.assertEqual(1, len(violations), violations)
        self.assertIn("not a declared adopted volume root", violations[0])

    def test_a_declared_authority_makes_the_fast_path_valid(self) -> None:
        self.assertEqual(
            [],
            validate_pod_volume_ownership(
                pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY),
                owned_roots=("/workspace",),
            ),
        )

    def test_a_descendant_of_an_owned_root_is_not_itself_owned(self) -> None:
        """The kubelet compares the mounted volume root, so an authority over
        /workspace says nothing about a pod that mounts /workspace/runs: that
        pod's own volume root is still unadopted."""

        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["containers"][0]["volumeMounts"][1]["mountPath"] = "/workspace/runs"
        violations = validate_pod_volume_ownership(pod, owned_roots=("/workspace",))
        self.assertTrue(
            any("not a declared adopted volume root" in item for item in violations),
            violations,
        )

    def test_a_sibling_path_is_not_covered_by_a_prefix_match(self) -> None:
        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["containers"][0]["volumeMounts"][1]["mountPath"] = "/workspace-scratch"
        violations = validate_pod_volume_ownership(pod, owned_roots=("/workspace",))
        self.assertTrue(
            any("not a declared adopted volume root" in item for item in violations)
        )

    def test_a_writable_subpath_mount_is_refused_outright(self) -> None:
        """A subPath hides the volume root, so nothing the pod can see proves
        the root the kubelet will check was ever adopted."""

        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["containers"][0]["volumeMounts"][1]["subPath"] = "runs"
        violations = validate_pod_volume_ownership(pod, owned_roots=("/workspace",))
        self.assertTrue(any("uses a subPath" in item for item in violations), violations)

    def test_a_read_only_mount_still_needs_an_agreed_producer(self) -> None:
        """fsGroup ownership is applied to the volume, not to the mount, so a
        read-only consumer reads through the same group and depends on the same
        agreement. Exempting it was the defect this assertion replaces."""

        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["containers"][0]["volumeMounts"][1]["readOnly"] = True
        self.assertTrue(validate_pod_volume_ownership(pod))
        self.assertEqual(
            [], validate_pod_volume_ownership(pod, owned_roots=("/workspace",))
        )

    def test_claim_level_read_only_beside_a_writable_mount_is_rejected(self) -> None:
        """The exact defect recorded as mosaic attempt-2: a readOnly claim marks
        the whole CSI attachment read-only and the writable sibling mount fails."""

        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["volumes"][1]["persistentVolumeClaim"]["readOnly"] = True
        pod["containers"][0]["volumeMounts"].append(
            {"name": "workspace", "mountPath": "/opt/fs2/artifacts", "readOnly": True}
        )
        violations = validate_pod_volume_ownership(pod, owned_roots=("/workspace",))
        self.assertTrue(any("marks the whole claim readOnly" in item for item in violations))

    def test_an_immutable_mount_must_declare_read_only(self) -> None:
        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["volumes"].append(
            {"name": "artifacts", "persistentVolumeClaim": {"claimName": "fs2-artifacts"}}
        )
        pod["containers"][0]["volumeMounts"].append(
            {"name": "artifacts", "mountPath": "/opt/fs2/artifacts"}
        )
        violations = validate_pod_volume_ownership(
            pod,
            owned_roots=("/workspace",),
            read_only_mount_paths=("/opt/fs2/artifacts",),
        )
        self.assertTrue(any("must set readOnly on the mount" in item for item in violations))

    def test_init_container_mounts_are_counted(self) -> None:
        pod = pod_with_workspace(policy=FS_GROUP_CHANGE_POLICY)
        pod["initContainers"] = [
            {
                "name": "stage",
                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
            }
        ]
        mounts = persistent_mounts(pod)
        self.assertEqual(["stage", "batch"], [mount["container"] for mount in mounts])

    def test_assert_raises_with_the_subject_named(self) -> None:
        with self.assertRaises(VolumeOwnershipError) as caught:
            assert_pod_volume_ownership(pod_with_workspace(), subject="mosaic stage design-000")
        self.assertIn("mosaic stage design-000", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class ProducerConsumerAuthorityTests(unittest.TestCase):
    def producer(self, **overrides):
        arguments = {
            "name": "acquisition Job",
            "gid": 10001,
            "root": "/mnt/fs2-serve-cache",
            "whole_tree": True,
        }
        arguments.update(overrides)
        return FilesystemGroupProducer(**arguments)

    def consumer(self, **overrides):
        arguments = {
            "name": "serving pod",
            "fs_group": 10001,
            "root": "/mnt/fs2-serve-cache",
            "read_only": True,
        }
        arguments.update(overrides)
        return FilesystemGroupConsumer(**arguments)

    def test_exact_agreement_is_an_authority(self) -> None:
        self.assertEqual([], authority_violations(self.producer(), self.consumer()))

    def test_a_group_mismatch_is_refused(self) -> None:
        violations = authority_violations(self.producer(), self.consumer(fs_group=1000))
        self.assertTrue(any("cannot match both" in item for item in violations), violations)

    def test_a_root_mismatch_is_refused(self) -> None:
        violations = authority_violations(
            self.producer(), self.consumer(root="/models")
        )
        self.assertTrue(
            any("only compares the mounted volume root" in item for item in violations)
        )

    def test_a_partial_producer_is_refused(self) -> None:
        violations = authority_violations(
            self.producer(whole_tree=False), self.consumer()
        )
        self.assertTrue(
            any("does not own every inode" in item for item in violations), violations
        )

    def test_a_read_only_consumer_is_not_exempt(self) -> None:
        """fsGroup ownership is applied per volume, not per mount, so a reader
        depends on the same agreement a writer does."""

        violations = authority_violations(
            self.producer(), self.consumer(fs_group=1000, read_only=True)
        )
        self.assertTrue(violations)

    def test_assert_names_the_consumer_and_the_policy(self) -> None:
        with self.assertRaises(VolumeOwnershipError) as caught:
            assert_authority(self.producer(), self.consumer(fs_group=1000))
        self.assertIn("serving pod", str(caught.exception))
        self.assertIn(FS_GROUP_CHANGE_POLICY, str(caught.exception))


class LiveChainContinuityTests(unittest.TestCase):
    """The acquisition -> localization -> serving chain, as it exists today.

    Both identities are read from the real renderers rather than typed here, so
    this cannot keep asserting a stale number after someone changes one of them.
    When the chain is deliberately aligned these assertions fail, and that is the
    signal that the policy can be turned on.
    """

    @staticmethod
    def serving_pod_security() -> dict:
        """The pod securityContext workloads.py actually renders."""

        from fs2_serve_catalog import workloads  # noqa: PLC0415

        source = Path(workloads.__file__).read_text(encoding="utf-8")
        # The pod-level context is the one carrying fsGroup; the container-level
        # block above it has no identity fields at all.
        anchor = source.index('"fsGroup"')
        block = source[source.rindex('"securityContext": {', 0, anchor) : anchor]
        block += source[anchor : source.index("}", anchor)]
        found = re.findall(r'"(runAsUser|runAsGroup|fsGroup)":\s*(\d+)', block)
        return {key: int(value) for key, value in found}

    def test_the_serving_consumer_identity_is_a_single_pinned_group(self) -> None:
        security = self.serving_pod_security()
        self.assertEqual(
            {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
            security,
            "workloads.py no longer renders the identity this chain was measured "
            "against; re-derive the authority before trusting the assertions below",
        )

    def test_the_acquisition_producer_and_serving_consumer_disagree(self) -> None:
        from fs2_serve_catalog.acquisition import (  # noqa: PLC0415
            ACQUISITION_FS_GROUP,
            ACQUISITION_RUN_AS_GID,
            ACQUISITION_RUN_AS_UID,
        )

        self.assertEqual(ACQUISITION_RUN_AS_UID, ACQUISITION_RUN_AS_GID)
        self.assertEqual(ACQUISITION_RUN_AS_GID, ACQUISITION_FS_GROUP)
        consumer_gid = self.serving_pod_security()["fsGroup"]

        violations = authority_violations(
            FilesystemGroupProducer(
                name="artifact acquisition Job",
                gid=ACQUISITION_FS_GROUP,
                root="/mnt/fs2-serve-cache",
                whole_tree=True,
            ),
            FilesystemGroupConsumer(
                name="model runtime pod",
                fs_group=consumer_gid,
                root="/mnt/fs2-serve-cache",
                read_only=True,
            ),
        )
        self.assertNotEqual(
            ACQUISITION_FS_GROUP,
            consumer_gid,
            "producer and consumer now agree; this test is the checklist for "
            "enabling OnRootMismatch, so work through the register's follow_up",
        )
        self.assertEqual(1, len(violations), violations)
        self.assertIn(f"gid {ACQUISITION_FS_GROUP}", violations[0])
        self.assertIn(f"fsGroup {consumer_gid}", violations[0])

    def test_the_localization_writer_identity_is_not_frozen(self) -> None:
        """render_localization_job inherits render_async_job's pod security
        context, which pins neither runAsUser nor runAsGroup, so the group its
        staged tree ends up owned by is undefined. The validator must say so
        rather than pass it silently."""

        from fs2_serve_catalog import kubernetes  # noqa: PLC0415

        source = Path(kubernetes.__file__).read_text(encoding="utf-8")
        block = source[source.index("pod_security: dict[str, Any] = {") :][:200]
        for field in ("runAsUser", "runAsGroup", "fsGroup"):
            self.assertNotIn(field, block)

        localizer = {
            "securityContext": {"runAsNonRoot": True},
            "containers": [
                {
                    "name": "cache",
                    "volumeMounts": [
                        {"name": "shared", "mountPath": "/mnt/shared", "readOnly": True},
                        {"name": "local", "mountPath": "/mnt/local"},
                    ],
                }
            ],
            "volumes": [
                {"name": "shared", "persistentVolumeClaim": {"claimName": "shared"}},
                {"name": "local", "persistentVolumeClaim": {"claimName": "local"}},
            ],
        }
        violations = validate_pod_volume_ownership(localizer)
        self.assertTrue(
            any("no frozen writer identity" in item for item in violations), violations
        )

    def test_no_renderer_claims_an_authority_it_cannot_prove(self) -> None:
        """Zero callers is the deliberate state: the contract ships, the rollout
        does not, and neither renderer was modified."""

        from fs2_serve_catalog import kubernetes, workloads  # noqa: PLC0415

        for module in (kubernetes, workloads):
            source = Path(module.__file__).read_text(encoding="utf-8")
            with self.subTest(module=module.__name__):
                self.assertNotIn("volume_ownership", source)
