from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from fs2_serve_catalog.acquisition import (
    ACQUISITION_FS_GROUP,
    ACQUISITION_RUN_AS_GID,
    ACQUISITION_RUN_AS_UID,
    _fresh_filesystem_write_proof,
    _materialize_exact_payload,
    acquire_huggingface_artifact,
    finalize_acquisition_receipt,
)
from fs2_serve_catalog.artifacts import (
    build_artifact_manifest,
    canonical_bytes,
    load_artifact_manifest,
    verify_artifact_tree,
)
from fs2_serve_catalog.loader import CatalogError, load_catalog
from fs2_serve_catalog.staging import stage_artifact

NODE = {
    "serving_node_name": "fs2-b300-unit-node",
    "serving_node_uid": "99999999-9999-9999-9999-999999999999",
    "serving_node_provider_id_sha256": hashlib.sha256(b"provider-id").hexdigest(),
}


def acquisition_identity() -> tuple[int, int, tuple[int, ...]]:
    return (
        ACQUISITION_RUN_AS_UID,
        ACQUISITION_RUN_AS_GID,
        (ACQUISITION_FS_GROUP,),
    )


def acquisition_workload(record, plan, operation_id="unit-acquire") -> dict[str, str]:
    helper = plan.to_dict()["helper_image"]
    image_digest = "sha256:" + hashlib.sha256(b"acquisition-helper-image").hexdigest()
    job_name = f"{record.model_id}-cache-{operation_id}"
    return {
        "FS2_ACQUISITION_OPERATION_ID": operation_id,
        "FS2_ACQUISITION_JOB_NAMESPACE": "fs2-models",
        "FS2_ACQUISITION_JOB_NAME": job_name,
        "FS2_ACQUISITION_JOB_UID": "10101010-1010-1010-1010-101010101010",
        "FS2_ACQUISITION_POD_NAME": job_name + "-unit00",
        "FS2_ACQUISITION_POD_UID": "20202020-2020-2020-2020-202020202020",
        "FS2_ACQUISITION_HELPER_IMAGE": (
            "registry.invalid/fs2-serve/acquisition-helper@" + image_digest
        ),
        "FS2_ACQUISITION_HELPER_IMAGE_DIGEST": image_digest,
        "FS2_ACQUISITION_HELPER_ADMISSION_DIGEST": hashlib.sha256(
            b"helper-admission"
        ).hexdigest(),
        "FS2_ACQUISITION_HELPER_REGISTRY_IDENTITY_SHA256": hashlib.sha256(
            b"helper-registry"
        ).hexdigest(),
        "FS2_ACQUISITION_HELPER_BUILD_IDENTITY_SHA256": hashlib.sha256(
            b"helper-build"
        ).hexdigest(),
        "FS2_ACQUISITION_PLAN_SHA256": hashlib.sha256(
            canonical_bytes(plan.to_dict())
        ).hexdigest(),
        "FS2_ACQUISITION_HELPER_CONTRACT_SHA256": hashlib.sha256(
            canonical_bytes(helper)
        ).hexdigest(),
    }


class ArtifactStagingTests(unittest.TestCase):
    def build(self, root: Path):
        source = root / "source"
        source.mkdir()
        (source / "a.bin").write_bytes(b"a" * 13)
        (source / "nested").mkdir()
        (source / "nested" / "b.bin").write_bytes(b"b" * 17)
        manifest = build_artifact_manifest(
            source,
            model_id="qwen3-8b",
            kind="weights",
            source_uri="hf://Qwen/Qwen3-8B",
            source_revision="b968826d9c46dd6066d109eabc6255188de91218",
            license_id="apache-2.0",
            license_state="verified",
            entitlement_state="not-required",
            owner="fs2-serve-localizer",
            retention="retained-platform",
        )
        return source, manifest

    def test_atomic_stage_is_content_addressed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self.build(root)
            cache = root / "cache" / "models" / "qwen3-8b"
            first = stage_artifact(
                manifest,
                source,
                cache,
                controller_owner="fs2-serve-localizer",
                **NODE,
                reserve_bytes=0,
                max_concurrent_files=2,
            )
            second = stage_artifact(
                manifest,
                source,
                cache,
                controller_owner="fs2-serve-localizer",
                **NODE,
                reserve_bytes=0,
                max_concurrent_files=1,
            )
            self.assertEqual("staged", first["outcome"])
            self.assertEqual("already-present", second["outcome"])
            self.assertEqual(
                "sfs://fs2-cache/mnt/fs2-serve-cache/models/qwen3-8b/sha256/"
                + manifest.content_digest,
                first["source_uri"],
            )
            self.assertEqual(
                "nvme://localhost/var/lib/fs2-serve/cache/models/qwen3-8b/sha256/"
                + manifest.content_digest,
                first["content_uri"],
            )
            self.assertNotIn(manifest.digest, first["content_uri"])
            self.assertTrue(first["cleanup"]["temporary_path_absent"])
            self.assertEqual(NODE["serving_node_uid"], first["serving_node"]["uid"])
            self.assertTrue(Path(first["lock_path"]).is_relative_to(cache))
            verify_artifact_tree(manifest, Path(first["destination_path"]))

    def test_manifest_round_trip_and_kind_prevent_subject_conflation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, manifest = self.build(root)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest.to_dict()) + "\n")
            loaded = load_artifact_manifest(path)
            self.assertEqual("weights", loaded.kind)
            self.assertEqual(manifest.digest, loaded.digest)
            try:
                from jsonschema import Draft202012Validator
            except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
                self.fail(f"jsonschema is required for artifact validation: {exc}")
            schema = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "schema"
                    / "artifact-manifest.schema.json"
                ).read_text()
            )
            Draft202012Validator(schema).validate(loaded.to_dict())
            value = loaded.to_dict()
            value["kind"] = "snapshot"
            path.write_text(json.dumps(value) + "\n")
            snapshot = load_artifact_manifest(path)
            self.assertNotEqual(manifest.digest, snapshot.digest)

    def test_manifest_preserves_empty_markers_but_requires_positive_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mixed"
            source.mkdir()
            (source / "download.complete").touch()
            (source / "weights.bin").write_bytes(b"weights")
            manifest = build_artifact_manifest(
                source,
                model_id="qwen3-8b",
                kind="nim-cache",
                source_uri="hf://Qwen/Qwen3-8B",
                source_revision="b968826d9c46dd6066d109eabc6255188de91218",
                license_id="apache-2.0",
                license_state="verified",
                entitlement_state="not-required",
                owner="fs2-serve-localizer",
                retention="ephemeral-test",
            )
            by_path = {item.path: item for item in manifest.files}
            self.assertEqual(by_path["download.complete"].bytes, 0)
            self.assertEqual(
                by_path["download.complete"].sha256,
                hashlib.sha256(b"").hexdigest(),
            )
            verify_artifact_tree(manifest, source)

            all_empty = root / "all-empty"
            all_empty.mkdir()
            (all_empty / "download.lock").touch()
            with self.assertRaisesRegex(CatalogError, "positive integer"):
                build_artifact_manifest(
                    all_empty,
                    model_id="qwen3-8b",
                    kind="nim-cache",
                    source_uri="hf://Qwen/Qwen3-8B",
                    source_revision="b968826d9c46dd6066d109eabc6255188de91218",
                    license_id="apache-2.0",
                    license_state="verified",
                    entitlement_state="not-required",
                    owner="fs2-serve-localizer",
                    retention="ephemeral-test",
                )

    def test_fallback_model_content_manifests_use_canonical_file_inventory(
        self,
    ) -> None:
        solution_root = Path(__file__).resolve().parents[3]
        cases = {
            "boltz2": {
                "path": solution_root
                / "models"
                / "bionemo"
                / "boltz2"
                / "artifact-manifest.json",
                "digest": "9459dd0c80992f21d07e70ae7d54c318e66a9d5202d6e849a134957b0740d82a",
                "expanded_bytes": 6204362719,
                "uri": "hf://boltz-community/boltz-2",
                "revision": "6fdef46d763fee7fbb83ca5501ccceff43b85607",
                "paths": ["boltz2_aff.ckpt", "boltz2_conf.ckpt", "mols.tar"],
            },
            "diffdock": {
                "path": solution_root
                / "models"
                / "structure"
                / "artifact-manifest.json",
                "digest": "fe05bca0521f77e120fe135021f555269b0128e929e60f201542d4c17483329c",
                "expanded_bytes": 2744924511,
                "uri": (
                    "oci://registry.example.invalid/k8s-inference/"
                    "fs2-models/diffdock@sha256:"
                    "cb3875f7d66b8d170d0e3f16d3d9a63aee8d63fbb23fdf65ec7ea0214d849529"
                ),
                "revision": "85c49b60d3e0b0182a59ee43a34a6d7036981284",
                "paths": [
                    "confidence_model/best_model_epoch75.pt",
                    "confidence_model/model_parameters.yml",
                    "esm2_t33_650M_UR50D-contact-regression.pt",
                    "esm2_t33_650M_UR50D.pt",
                    "score_model/best_ema_inference_epoch_model.pt",
                    "score_model/model_parameters.yml",
                ],
            },
        }
        for model_id, expected in cases.items():
            with self.subTest(model_id=model_id):
                manifest = load_artifact_manifest(expected["path"])
                inventory = [
                    {
                        "path": item.path,
                        "bytes": item.bytes,
                        "sha256": item.sha256,
                    }
                    for item in manifest.files
                ]
                self.assertEqual(model_id, manifest.model_id)
                self.assertEqual(expected["digest"], manifest.content_digest)
                self.assertEqual(expected["expanded_bytes"], manifest.expanded_bytes)
                self.assertEqual(expected["uri"], manifest.source_uri)
                self.assertEqual(expected["revision"], manifest.source_revision)
                self.assertEqual(
                    expected["paths"], [item["path"] for item in inventory]
                )
                self.assertEqual(
                    expected["digest"],
                    hashlib.sha256(canonical_bytes(inventory)).hexdigest(),
                )

                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "artifact-manifest.json"
                    for field, replacement in (
                        ("bytes", inventory[0]["bytes"] + 1),
                        ("sha256", hashlib.sha256(b"changed-file").hexdigest()),
                    ):
                        value = manifest.to_dict()
                        value["content"]["files"][0][field] = replacement
                        if field == "bytes":
                            value["content"]["expanded_bytes"] += 1
                        path.write_text(json.dumps(value) + "\n")
                        with self.subTest(model_id=model_id, mutation=field):
                            with self.assertRaisesRegex(
                                CatalogError,
                                "content digest does not match the file inventory",
                            ):
                                load_artifact_manifest(path)

                    value = manifest.to_dict()
                    value["content"]["files"][0:2] = reversed(
                        value["content"]["files"][0:2]
                    )
                    path.write_text(json.dumps(value) + "\n")
                    with self.assertRaisesRegex(
                        CatalogError, "sorted by canonical path"
                    ):
                        load_artifact_manifest(path)

    def test_sfs_and_nvme_uris_reject_aliases_double_slashes_and_traversal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, manifest = self.build(root)
            path = root / "manifest.json"
            base = manifest.to_dict()
            canonical = (
                "sfs://fs2-cache/mnt/fs2-serve-cache/models/qwen3-8b/sha256/"
                + manifest.content_digest
            )
            base["source"]["uri"] = canonical
            path.write_text(json.dumps(base) + "\n")
            self.assertEqual(canonical, load_artifact_manifest(path).source_uri)
            for uri in (
                canonical.replace("fs2-cache", "other-cache"),
                canonical.replace("/models/", "//models/"),
                canonical.replace("/models/", "/models/../models/"),
                canonical.replace("sfs://", "nvme://"),
            ):
                with self.subTest(uri=uri):
                    value = manifest.to_dict()
                    value["source"]["uri"] = uri
                    path.write_text(json.dumps(value) + "\n")
                    with self.assertRaises(CatalogError):
                        load_artifact_manifest(path)

    def test_provider_block_uri_is_exact_claim_scoped_and_content_addressed(
        self,
    ) -> None:
        from fs2_serve_catalog.loader import canonical_content_uri

        digest = hashlib.sha256(b"provider-block-content").hexdigest()
        canonical = "pvc://fs2-models/qwen3-8b-weights/models/qwen3-8b/sha256/" + digest
        self.assertEqual(
            canonical,
            canonical_content_uri(
                canonical,
                model_id="qwen3-8b",
                content_digest=digest,
                scheme="pvc",
            ),
        )
        for value in (
            canonical.replace("qwen3-8b-weights", "fs2-cache"),
            canonical.replace("/models/", "//models/"),
            canonical.replace("/models/", "/models/../models/"),
            canonical.replace("pvc://", "sfs://"),
        ):
            with self.subTest(value=value), self.assertRaises(CatalogError):
                canonical_content_uri(
                    value,
                    model_id="qwen3-8b",
                    content_digest=digest,
                    scheme="pvc",
                )

    def test_foreign_controller_insufficient_space_and_symlink_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest = self.build(root)
            cache = root / "cache" / "models" / "qwen3-8b"
            with self.assertRaisesRegex(CatalogError, "explicitly owned"):
                stage_artifact(
                    manifest,
                    source,
                    cache,
                    controller_owner="nim-operator-nimcache",
                    **NODE,
                )
            with self.assertRaisesRegex(CatalogError, "insufficient free space"):
                stage_artifact(
                    manifest,
                    source,
                    cache,
                    controller_owner="fs2-serve-localizer",
                    **NODE,
                    reserve_bytes=10**30,
                )
            (source / "link").symlink_to(source / "a.bin")
            with self.assertRaisesRegex(CatalogError, "non-regular"):
                verify_artifact_tree(manifest, source)

    def test_public_hf_acquisition_is_exact_revision_credentialless_and_atomic(
        self,
    ) -> None:
        catalog_root = Path(__file__).resolve().parents[1]
        catalog = load_catalog(catalog_root, repo_root=catalog_root / "packaged-repository")
        record = catalog.model("nv-reason-cxr-3b")
        plan = catalog.acquisition_plan("nv-reason-cxr-3b")
        calls = []

        def fake_download(**kwargs):
            calls.append(kwargs)
            payload = Path(kwargs["local_dir"])
            payload.mkdir(parents=True)
            (payload / "weights.bin").write_bytes(b"exact-public-weights")
            return str(payload)

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "models" / "nv-reason-cxr-3b"
            first = acquire_huggingface_artifact(
                record,
                plan,
                destination,
                snapshot_download=fake_download,
                process_identity_probe=acquisition_identity,
                workload_identity_probe=lambda: acquisition_workload(record, plan),
            )
            second = acquire_huggingface_artifact(
                record,
                plan,
                destination,
                snapshot_download=fake_download,
                process_identity_probe=acquisition_identity,
                workload_identity_probe=lambda: acquisition_workload(record, plan),
            )
            self.assertEqual("acquired", first["outcome"])
            self.assertEqual("already-present", second["outcome"])
            self.assertEqual(
                {
                    "repo_id": "nvidia/NV-Reason-CXR-3B",
                    "revision": "056bd0383b35226554da9dc5866e095df174ae19",
                    "local_dir": calls[0]["local_dir"],
                    "token": False,
                },
                calls[0],
            )
            unsigned = dict(first)
            digest = unsigned.pop("receipt_digest")
            self.assertEqual(
                hashlib.sha256(canonical_bytes(unsigned)).hexdigest(), digest
            )
            self.assertTrue(first["cleanup"]["temporary_path_absent"])
            self.assertEqual(
                "fs2-serve.nebius.ai/artifact-acquisition-worker-result/v1",
                first["schema"],
            )
            self.assertEqual("none-public-revision", first["credential_source"])
            self.assertFalse(first["token_used"])
            self.assertEqual(
                (True, 10001, 10001, 10001, "Strict", "RuntimeDefault"),
                tuple(
                    first["execution"][key]
                    for key in (
                        "run_as_non_root",
                        "run_as_uid",
                        "run_as_gid",
                        "fs_group",
                        "supplemental_groups_policy",
                        "seccomp_profile",
                    )
                ),
            )
            self.assertIsNone(first["filesystem_write_proof"])
            self.assertNotIn("credential_value", first)
            manifest_path = (
                destination.parent
                / ".manifests"
                / f"{first['artifact_manifest_digest']}.json"
            )
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(
                (
                    destination
                    / "sha256"
                    / first["artifact_content_digest"]
                    / "weights.bin"
                ).is_file()
            )
            try:
                from jsonschema import Draft202012Validator
            except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
                self.fail(f"jsonschema is required for acquisition validation: {exc}")
            schema = json.loads(
                (
                    catalog_root
                    / "schema"
                    / "artifact-acquisition-worker-result.schema.json"
                ).read_text()
            )
            Draft202012Validator(schema).validate(first)
            expected_resources = [
                {
                    key: first["execution"]["job"][key]
                    for key in ("api_version", "kind", "namespace", "name", "uid")
                },
                dict(first["execution"]["pod"]),
            ]
            cleanup = {
                "completed_at": "2026-08-27T12:00:00Z",
                "observer_identity_sha256": hashlib.sha256(
                    b"cleanup-observer"
                ).hexdigest(),
                "controller_identity_sha256": hashlib.sha256(
                    b"cleanup-controller"
                ).hexdigest(),
                "api_server_observed": True,
                "expected_resources": expected_resources,
                "resources": [
                    {
                        **item,
                        "delete_precondition_uid": item["uid"],
                        "final_state": "absent",
                        "replacement_uid": None,
                        "replacement_touched": False,
                    }
                    for item in expected_resources
                ],
                "temporary_path_absent": True,
                "write_marker_absent": True,
                "foreign_uids_touched": False,
            }
            final = finalize_acquisition_receipt(record, plan, first, cleanup)
            self.assertEqual(
                "fs2-serve.nebius.ai/artifact-acquisition-receipt/v4",
                final["schema"],
            )
            final_schema = json.loads(
                (
                    catalog_root / "schema" / "artifact-acquisition-receipt.schema.json"
                ).read_text()
            )
            Draft202012Validator(final_schema).validate(final)
            wrong_uid = json.loads(json.dumps(cleanup))
            wrong_uid["resources"][0]["delete_precondition_uid"] = (
                "30303030-3030-3030-3030-303030303030"
            )
            with self.assertRaisesRegex(CatalogError, "UID-fenced"):
                finalize_acquisition_receipt(record, plan, first, wrong_uid)
            replacement = json.loads(json.dumps(cleanup))
            replacement["resources"][1]["replacement_uid"] = expected_resources[1][
                "uid"
            ]
            with self.assertRaisesRegex(CatalogError, "replacement-safe"):
                finalize_acquisition_receipt(record, plan, first, replacement)

    def test_qwen_clean_payload_uses_exact_allowlist_and_drops_hf_metadata(
        self,
    ) -> None:
        files = [
            {
                "path": "config.json",
                "bytes": 6,
                "sha256": hashlib.sha256(b"config").hexdigest(),
            },
            {
                "path": "nested/model.bin",
                "bytes": 7,
                "sha256": hashlib.sha256(b"weights").hexdigest(),
            },
        ]
        expected = {"file_count": 2, "files": files}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            payload = root / "payload"
            (scratch / "nested").mkdir(parents=True)
            (scratch / ".cache" / "huggingface").mkdir(parents=True)
            (scratch / "config.json").write_bytes(b"config")
            (scratch / "nested" / "model.bin").write_bytes(b"weights")
            (scratch / ".cache" / "huggingface" / "download.json").write_text("{}")
            payload.mkdir()
            _materialize_exact_payload(scratch, payload, expected)
            self.assertEqual(
                ["config.json", "nested/model.bin"],
                sorted(
                    item.relative_to(payload).as_posix()
                    for item in payload.rglob("*")
                    if item.is_file()
                ),
            )
            self.assertFalse((payload / ".cache").exists())

            (scratch / "extra.bin").write_bytes(b"extra")
            with self.assertRaisesRegex(CatalogError, "extra artifact entry"):
                _materialize_exact_payload(scratch, root / "payload-extra", expected)
            (scratch / "extra.bin").unlink()
            (scratch / "link.bin").symlink_to(scratch / "config.json")
            with self.assertRaisesRegex(CatalogError, "extra artifact entry"):
                _materialize_exact_payload(scratch, root / "payload-link", expected)

    def test_qwen_expected_identity_is_a_gate_not_fake_promotion_evidence(self) -> None:
        catalog_root = Path(__file__).resolve().parents[1]
        catalog = load_catalog(catalog_root, repo_root=catalog_root / "packaged-repository")
        record = catalog.model("qwen3-8b")
        plan = catalog.acquisition_plan("qwen3-8b")

        def fake_download(**kwargs):
            payload = Path(kwargs["local_dir"])
            payload.mkdir(parents=True)
            (payload / "weights.bin").write_bytes(b"not-the-reviewed-tree")
            return str(payload)

        with tempfile.TemporaryDirectory() as temporary:
            proof = {
                "filesystem_type": "ext4",
                "probe_path": str(Path(temporary) / "models" / "qwen3-8b"),
                "operation": "exclusive-create-write-fsync-read-unlink",
                "bytes_written": 40,
                "payload_sha256": hashlib.sha256(
                    b"fs2-provider-block-fresh-write-proof/v1\n"
                ).hexdigest(),
                "file_uid": 10001,
                "file_gid": 10001,
                "file_mode": "0600",
                "marker_removed": True,
                "directory_fsync": True,
            }
            with self.assertRaisesRegex(CatalogError, "extra artifact entry"):
                acquire_huggingface_artifact(
                    record,
                    plan,
                    Path(temporary) / "models" / "qwen3-8b",
                    snapshot_download=fake_download,
                    reserve_bytes=0,
                    process_identity_probe=acquisition_identity,
                    workload_identity_probe=lambda: acquisition_workload(record, plan),
                    filesystem_type_probe=lambda _: "ext4",
                    fresh_write_proof=lambda *_args, **_kwargs: proof,
                )

    def test_fresh_write_proof_is_exclusive_durable_read_back_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            proof = _fresh_filesystem_write_proof(
                destination,
                "unit-operation",
                filesystem_type="ext4",
            )
            self.assertEqual(
                "exclusive-create-write-fsync-read-unlink", proof["operation"]
            )
            self.assertEqual("ext4", proof["filesystem_type"])
            self.assertEqual(0o600, int(proof["file_mode"], 8))
            self.assertEqual(40, proof["bytes_written"])
            self.assertTrue(proof["marker_removed"])
            self.assertTrue(proof["directory_fsync"])
            self.assertFalse((destination / ".fs2-write-proof-unit-operation").exists())

    def test_blocked_or_ngc_plan_cannot_use_public_hf_acquisition(self) -> None:
        catalog_root = Path(__file__).resolve().parents[1]
        catalog = load_catalog(catalog_root, repo_root=catalog_root / "packaged-repository")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CatalogError, "exact model plan"):
                acquire_huggingface_artifact(
                    catalog.model("boltz2"),
                    catalog.acquisition_plan("boltz2"),
                    Path(temporary) / "models" / "boltz2",
                    snapshot_download=lambda **_: "unused",
                )


if __name__ == "__main__":
    unittest.main()
