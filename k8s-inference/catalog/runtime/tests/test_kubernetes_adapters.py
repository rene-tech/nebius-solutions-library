from __future__ import annotations

import copy
import json
import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fs2_serve_catalog.capabilities import (
    GPU_TOLERATION,
    bind_backend_capability,
)
from fs2_serve_catalog.kubernetes import (
    ASYNC_JOB_KINDS,
    render_async_job,
    render_artifact_acquisition_job,
    render_image_prepull_job,
    render_local_queue,
    render_local_queues,
    render_localization_job,
    render_ngc_target_node_canary_job,
    render_provider_block_pvc,
)
from fs2_serve_catalog.loader import CatalogError, load_catalog
from fs2_serve_catalog.loader import ModelRecord
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)
from fs2_serve_catalog.evidence import (
    load_acquisition_helper_image_admission,
    load_protected_storage_class_admission,
    load_provider_block_writer_admission,
)
from fs2_serve_catalog.prerequisites import bind_runtime_prerequisites
from fs2_serve_catalog.workloads import (
    replica_field_ownership,
    render_kserve_standard_workload,
    render_native_http_workload,
    render_nim_operator_cache,
    render_nim_operator_service,
)
from tests.test_ngc_fixtures import make_ngc_materialization


CATALOG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CATALOG_ROOT / "packaged-repository"


class KubernetesAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        resources = []
        for ordinal, requirement_id in enumerate(
            sorted(cls.catalog.runtime_prerequisites), start=1
        ):
            requirement = cls.catalog.prerequisite(requirement_id).to_dict()
            kind = requirement["kind"]
            resources.append(
                {
                    "id": requirement_id,
                    "api_version": requirement["api_version"],
                    "kind": kind,
                    "namespace": requirement["namespace"],
                    "name": requirement["name"],
                    "uid": f"{ordinal:08x}-aaaa-bbbb-cccc-{ordinal:012x}",
                    "resource_version": str(ordinal),
                    "state": "Bound" if kind == "PersistentVolumeClaim" else "present",
                    "secret_type": requirement["secret_type"],
                    "data_keys": requirement["required_keys"],
                    "access_modes": requirement["access_modes"],
                    "capacity_bytes": (
                        requirement["minimum_capacity_bytes"]
                        if kind == "PersistentVolumeClaim"
                        else None
                    ),
                }
            )
        ngc_resources = {
            item["id"]: item
            for item in resources
            if item["id"]
            in {"fs2-models/ngc-pull-secret", "fs2-models/ngc-runtime-secret"}
        }
        ngc_materialization = make_ngc_materialization(ngc_resources)
        cls.observation = {
            "schema": "fs2-serve.nebius.ai/observed-prerequisites/v4",
            "values_suppressed": True,
            "legacy_ngc_secret_copied": False,
            "legacy_plaintext_rotation_source_used": False,
            "legacy_phase_7c_hmac_reused": False,
            "exposed_evo_bearer_reused": False,
            "ngc_credential_materialization": ngc_materialization,
            "resources": resources,
        }
        cls.prerequisites = bind_runtime_prerequisites(cls.catalog, cls.observation)

    @staticmethod
    def digest(label: str, *, image: bool = False) -> str:
        value = hashlib.sha256(label.encode()).hexdigest()
        return "sha256:" + value if image else value

    def resolved_qwen(self) -> ModelRecord:
        original = self.catalog.model("qwen3-8b")
        value = original.to_dict()
        image_digest = self.digest("qwen-platform-runtime", image=True)
        value["runtime"]["image"] = {
            "reference": "registry.invalid/fs2/qwen@" + image_digest,
            "digest": image_digest,
            "state": "resolved",
        }
        return ModelRecord(
            model_id=original.model_id,
            path=original.path,
            digest=hashlib.sha256(canonical_bytes(value)).hexdigest(),
            _value=value,
        )

    def provider_admissions(self, record, operation_id="qwen-acquire-1"):
        """Create signed unit-only pre-PVC and single-writer admissions."""

        private_key = Ed25519PrivateKey.generate()
        key_id = public_key_id(private_key.public_key())
        trusted = {key_id: public_key_value(private_key.public_key())}
        session_id = self.digest("provider-admission-session")

        def write_receipt(root, kind, unsigned, claims, nonce_label):
            digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
            value = {**unsigned, "receipt_digest": digest}
            directory = root / kind
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{digest}.json").write_text(json.dumps(value) + "\n")
            attestation = create_signed_attestation(
                private_key=private_key,
                session_id=session_id,
                nonce=self.digest(nonce_label),
                issued_at="2026-08-26T21:49:35Z",
                expires_at="2026-08-26T23:00:00Z",
                kind=kind,
                subject_schema=unsigned["schema"],
                subject_digest=digest,
                model_id=record.model_id,
                claims=claims,
            )
            attestation_directory = root / "attestations" / kind
            attestation_directory.mkdir(parents=True, exist_ok=True)
            (attestation_directory / f"{digest}.json").write_text(
                json.dumps(attestation) + "\n"
            )
            return digest

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage_class = {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {
                    "name": "fs2-network-ssd-retain",
                    "uid": "55555555-5555-5555-5555-555555555555",
                    "resourceVersion": "11",
                },
                "spec": {
                    "provisioner": "compute.csi.nebius.com",
                    "reclaimPolicy": "Retain",
                    "volumeBindingMode": "WaitForFirstConsumer",
                    "allowVolumeExpansion": True,
                    "parameters": {
                        "type": "NETWORK_SSD",
                        "csi.storage.k8s.io/fstype": "ext4",
                    },
                },
            }
            observer = {
                "source": "kubernetes-apiserver-get",
                "api_server_observed": True,
                "cluster_identity_sha256": self.digest("provider-cluster"),
                "api_server_identity_sha256": self.digest("provider-apiserver"),
                "service_account_namespace": "fs2-system",
                "service_account_name": "fs2-storage-contract-observer",
                "service_account_uid": "12121212-1212-1212-1212-121212121212",
            }
            intended_claim = {
                "namespace": "fs2-models",
                "name": "qwen3-8b-weights",
                "model_id": record.model_id,
                "model_digest": record.digest,
            }
            storage_digest = write_receipt(
                root,
                "protected-storage-classes",
                {
                    "schema": "fs2-serve.nebius.ai/protected-storage-class-receipt/v1",
                    "status": "PASS",
                    "observed_at": "2026-08-26T21:45:00Z",
                    "model_id": record.model_id,
                    "model_digest": record.digest,
                    "observer": observer,
                    "storage_class": storage_class,
                    "intended_claim": intended_claim,
                },
                {
                    "model_digest": record.digest,
                    "storage_class_identity_sha256": hashlib.sha256(
                        canonical_bytes(storage_class)
                    ).hexdigest(),
                    "intended_claim_sha256": hashlib.sha256(
                        canonical_bytes(intended_claim)
                    ).hexdigest(),
                    "observer_identity_sha256": hashlib.sha256(
                        canonical_bytes(observer)
                    ).hexdigest(),
                },
                "protected-storage-class",
            )
            validation_time = datetime(
                2026, 8, 26, 21, 49, 40, tzinfo=timezone.utc
            )
            for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
                path.chmod(0o750 if path.is_dir() else 0o640)
            root.chmod(0o700)
            storage_admission = load_protected_storage_class_admission(
                record,
                root,
                receipt_digest=storage_digest,
                evidence_session_id=session_id,
                trusted_attestors=trusted,
                validation_time=validation_time,
            )
            service_account_uid = self.prerequisites.resource(
                "fs2-models/cache-service-account"
            )["uid"]
            job_name = f"{record.model_id}-cache-{operation_id}"
            claim = {
                "namespace": "fs2-models",
                "name": "qwen3-8b-weights",
                "uid": "44444444-4444-4444-4444-444444444444",
                "resource_version": "17",
            }
            writer = {
                "api_version": "batch/v1",
                "kind": "Job",
                "namespace": "fs2-models",
                "name": job_name,
                "service_account_name": "cache-service-account",
                "service_account_uid": service_account_uid,
            }
            controller_subject = {
                "namespace": "fs2-system",
                "deployment_name": "fs2-provider-block-writer-admission",
                "service_account_name": "fs2-provider-block-writer-admission",
                "validating_admission_policy_name": "fs2-provider-block-sole-writer",
                "deployment_uid": "13131313-1313-1313-1313-131313131313",
                "pod_name": "fs2-provider-block-writer-admission-0",
                "pod_uid": "12121212-1212-1212-1212-121212121212",
                "pod_owner_deployment_uid": "13131313-1313-1313-1313-131313131313",
                "service_account_uid": "14141414-1414-1414-1414-141414141414",
                "validating_admission_policy_uid": (
                    "15151515-1515-1515-1515-151515151515"
                ),
                "writer_create_role_name": "fs2-provider-block-writer-create",
                "writer_create_role_uid": "18181818-1818-1818-1818-181818181818",
                "writer_create_role_binding_name": "fs2-provider-block-writer-create",
                "writer_create_role_binding_uid": (
                    "19191919-1919-1919-1919-191919191919"
                ),
            }
            controller = {
                **controller_subject,
                "identity_sha256": hashlib.sha256(
                    canonical_bytes(controller_subject)
                ).hexdigest(),
            }
            lease = {
                "api_version": "coordination.k8s.io/v1",
                "kind": "Lease",
                "namespace": "fs2-models",
                "name": "qwen3-8b-weights-writer",
                "uid": "16161616-1616-1616-1616-161616161616",
                "resource_version": "18",
                "holder_identity": f"{operation_id}:{job_name}",
                "fencing_token": 23,
                "renew_time": "2026-08-26T21:49:00Z",
                "lease_duration_seconds": 900,
            }
            mount_set = {
                "api_server_identity_sha256": observer[
                    "api_server_identity_sha256"
                ],
                "namespace": "fs2-models",
                "claim_uid": claim["uid"],
                "list_resource_version": "16",
                "continue_token": None,
                "remaining_item_count": 0,
                "complete": True,
                "observed_at": "2026-08-26T21:49:20Z",
                "mounts": [],
            }
            mount_set_digest = hashlib.sha256(
                canonical_bytes(mount_set)
            ).hexdigest()
            api_fence = {
                "enforcement": (
                    "controller-owned-job-create-plus-validating-admission-policy-plus-lease-cas"
                ),
                "api_server_applied": True,
                "claim_resource_version": claim["resource_version"],
                "allowed_operation_id": operation_id,
                "allowed_writer_name": job_name,
                "allowed_creator_service_account_uid": controller[
                    "service_account_uid"
                ],
                "lease_uid": lease["uid"],
                "fencing_token": lease["fencing_token"],
                "complete_mount_set_sha256": mount_set_digest,
                "writer_create_role_uid": controller["writer_create_role_uid"],
                "writer_create_role_binding_uid": controller[
                    "writer_create_role_binding_uid"
                ],
                "race_window": (
                    "closed-by-controller-held-lease-through-job-create-and-completion"
                ),
                "second_writer_denied": True,
            }
            writer_digest = write_receipt(
                root,
                "provider-block-writer-admissions",
                {
                    "schema": "fs2-serve.nebius.ai/provider-block-writer-admission/v2",
                    "status": "admitted",
                    "admitted_at": "2026-08-26T21:49:30Z",
                    "model_id": record.model_id,
                    "model_digest": record.digest,
                    "operation_id": operation_id,
                    "storage_class_receipt_digest": storage_digest,
                    "claim": claim,
                    "writer": writer,
                    "controller": controller,
                    "lease": lease,
                    "mount_set": mount_set,
                    "api_fence": api_fence,
                },
                {
                    "model_digest": record.digest,
                    "storage_class_receipt_digest": storage_digest,
                    "operation_id": operation_id,
                    "claim_identity_sha256": hashlib.sha256(
                        canonical_bytes(claim)
                    ).hexdigest(),
                    "writer_identity_sha256": hashlib.sha256(
                        canonical_bytes(writer)
                    ).hexdigest(),
                    "controller_identity_sha256": controller["identity_sha256"],
                    "lease_identity_sha256": hashlib.sha256(
                        canonical_bytes(lease)
                    ).hexdigest(),
                    "complete_mount_set_sha256": mount_set_digest,
                    "api_fence_sha256": hashlib.sha256(
                        canonical_bytes(api_fence)
                    ).hexdigest(),
                },
                "provider-writer-admission",
            )
            for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
                path.chmod(0o750 if path.is_dir() else 0o640)
            root.chmod(0o700)
            writer_admission = load_provider_block_writer_admission(
                record,
                storage_admission,
                root,
                receipt_digest=writer_digest,
                evidence_session_id=session_id,
                trusted_attestors=trusted,
                validation_time=validation_time,
            )
        return storage_admission, writer_admission

    def helper_admission(self, record, *, mutate=None):
        """Create a signed unit-only helper-image supply-chain admission."""

        plan = self.catalog.acquisition_plan(record.model_id)
        helper = plan.to_dict()["helper_image"]
        private_key = Ed25519PrivateKey.generate()
        key_id = public_key_id(private_key.public_key())
        session_id = self.digest("helper-admission-session-" + record.model_id)
        image_digest = self.digest("helper-image", image=True)
        image = {
            "id": "fs2-acquisition-helper",
            "reference": "registry.invalid/fs2-serve/acquisition-helper@" + image_digest,
            "digest": image_digest,
            "registry_identity_sha256": self.digest("helper-registry"),
            "os": "linux",
            "architecture": "amd64",
        }
        build = {
            "repository": helper["build_source"]["repository"],
            "source_commit": hashlib.sha1(b"helper-source-commit").hexdigest(),
            "source_tree": hashlib.sha1(b"helper-source-tree").hexdigest(),
            "source_path": helper["build_source"]["path"],
            "package": helper["build_source"]["package"],
            "package_version": helper["build_source"]["package_version"],
            "wheel_sha256": self.digest("helper-wheel"),
            "pyproject_sha256": helper["build_source"]["pyproject_sha256"],
            "uv_lock_sha256": helper["build_source"]["uv_lock_sha256"],
            "entrypoint": helper["entrypoint"],
        }
        attestations = {
            "signature": {
                "verified": True,
                "subject_image_digest": image_digest,
                "registry_identity_sha256": image["registry_identity_sha256"],
                "bundle_sha256": self.digest("helper-signature-bundle"),
                "signer_identity_sha256": self.digest("helper-signer"),
            },
            "provenance": {
                "predicate_type": "https://slsa.dev/provenance/v1",
                "statement_sha256": self.digest("helper-provenance"),
                "subject_image_digest": image_digest,
                "source_commit": build["source_commit"],
                "source_tree": build["source_tree"],
                "build_identity_sha256": hashlib.sha256(
                    canonical_bytes(build)
                ).hexdigest(),
                "helper_contract_sha256": hashlib.sha256(
                    canonical_bytes(helper)
                ).hexdigest(),
                "builder_identity_sha256": self.digest("helper-builder"),
                "build_type": "https://fs2-serve.nebius.ai/build/container/v1",
                "materials_sha256": self.digest("helper-materials"),
                "all_container_images_digest_pinned": True,
            },
            "sbom": {
                "predicate_type": "https://spdx.dev/Document",
                "statement_sha256": self.digest("helper-sbom"),
                "subject_image_digest": image_digest,
                "package": build["package"],
                "package_version": build["package_version"],
                "wheel_sha256": build["wheel_sha256"],
            },
        }
        review = {
            "review_commit": hashlib.sha1(b"helper-review-commit").hexdigest(),
            "reviewer_identity_sha256": self.digest("helper-reviewer"),
        }
        unsigned = {
            "schema": "fs2-serve.nebius.ai/acquisition-helper-image-admission/v1",
            "status": "PASS",
            "verified_at": "2026-08-26T21:45:00Z",
            "valid_until": "2026-08-26T22:59:00Z",
            "model_id": record.model_id,
            "model_digest": record.digest,
            "acquisition_plan_sha256": hashlib.sha256(
                canonical_bytes(plan.to_dict())
            ).hexdigest(),
            "helper_contract_sha256": hashlib.sha256(
                canonical_bytes(helper)
            ).hexdigest(),
            "image": image,
            "build": build,
            "attestations": attestations,
            "review": review,
        }
        if mutate is not None:
            mutate(unsigned)
        digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        value = {**unsigned, "receipt_digest": digest}
        admitted_image = unsigned["image"]
        admitted_build = unsigned["build"]
        admitted_attestations = unsigned["attestations"]
        admitted_review = unsigned["review"]
        claims = {
            "model_digest": record.digest,
            "acquisition_plan_sha256": unsigned["acquisition_plan_sha256"],
            "helper_contract_sha256": unsigned["helper_contract_sha256"],
            "image_reference": admitted_image["reference"],
            "image_digest": admitted_image["digest"],
            "registry_identity_sha256": admitted_image["registry_identity_sha256"],
            "build_identity_sha256": hashlib.sha256(
                canonical_bytes(admitted_build)
            ).hexdigest(),
            "attestation_identity_sha256": hashlib.sha256(
                canonical_bytes(admitted_attestations)
            ).hexdigest(),
            "review_identity_sha256": hashlib.sha256(
                canonical_bytes(admitted_review)
            ).hexdigest(),
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "acquisition-helper-images"
            directory.mkdir(parents=True)
            (directory / f"{digest}.json").write_text(json.dumps(value) + "\n")
            signed = create_signed_attestation(
                private_key=private_key,
                session_id=session_id,
                nonce=self.digest("helper-admission-nonce-" + record.model_id),
                issued_at="2026-08-26T21:49:35Z",
                expires_at="2026-08-26T23:00:00Z",
                kind="acquisition-helper-images",
                subject_schema=unsigned["schema"],
                subject_digest=digest,
                model_id=record.model_id,
                claims=claims,
            )
            attestation_directory = root / "attestations" / "acquisition-helper-images"
            attestation_directory.mkdir(parents=True)
            (attestation_directory / f"{digest}.json").write_text(
                json.dumps(signed) + "\n"
            )
            for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
                path.chmod(0o750 if path.is_dir() else 0o640)
            root.chmod(0o700)
            return load_acquisition_helper_image_admission(
                record,
                plan,
                root,
                receipt_digest=digest,
                evidence_session_id=session_id,
                trusted_attestors={key_id: public_key_value(private_key.public_key())},
                validation_time=datetime(2026, 8, 26, 21, 49, 40, tzinfo=timezone.utc),
            )

    def test_ngc_materialization_binds_precreated_server_observed_secrets(self) -> None:
        materialization = self.observation["ngc_credential_materialization"]
        self.assertEqual(
            "securely-pre-created-existing-kubernetes-secrets",
            materialization["delivery_mode"],
        )
        self.assertIsNone(materialization["optional_backend_eligibility_receipt"])
        self.assertNotIn("secret_backend", materialization)
        observation = materialization["server_observation"]
        self.assertEqual(
            "authenticated-kubernetes-apiserver-get", observation["method"]
        )
        self.assertFalse(observation["values_recorded"])
        self.assertEqual(
            hashlib.sha256(canonical_bytes(observation)).hexdigest(),
            materialization["server_observation_sha256"],
        )
        self.assertEqual(
            ["fs2-ngc-pull", "fs2-ngc-runtime"],
            [item["name"] for item in materialization["secrets"]],
        )
        for secret in materialization["secrets"]:
            observed = self.prerequisites.resource(secret["requirement_id"])
            self.assertEqual(
                {
                    key: observed[key]
                    for key in (
                        "api_version",
                        "kind",
                        "namespace",
                        "name",
                        "uid",
                        "resource_version",
                        "secret_type",
                        "data_keys",
                    )
                },
                {
                    key: secret[key]
                    for key in (
                        "api_version",
                        "kind",
                        "namespace",
                        "name",
                        "uid",
                        "resource_version",
                        "secret_type",
                        "data_keys",
                    )
                },
            )

    def test_acquisition_helper_admission_rejects_supply_chain_substitution(self) -> None:
        record = self.catalog.model("nv-reason-cxr-3b")
        foreign_digest = self.digest("foreign-helper-image", image=True)

        def foreign_image(value):
            value["image"].update(
                {
                    "reference": "registry.invalid/foreign/helper@" + foreign_digest,
                    "digest": foreign_digest,
                }
            )
            value["attestations"]["signature"]["subject_image_digest"] = foreign_digest
            value["attestations"]["provenance"]["subject_image_digest"] = foreign_digest
            value["attestations"]["sbom"]["subject_image_digest"] = foreign_digest

        cases = {
            "foreign-image": foreign_image,
            "dummy-digest": lambda value: value["image"].update(
                {
                    "reference": (
                        "registry.invalid/fs2-serve/acquisition-helper@sha256:" + "f" * 64
                    ),
                    "digest": "sha256:" + "f" * 64,
                }
            ),
            "provenance-source": lambda value: value["attestations"][
                "provenance"
            ].update({"source_commit": hashlib.sha1(b"foreign-source").hexdigest()}),
            "mutable-base-material": lambda value: value["attestations"][
                "provenance"
            ].update({"all_container_images_digest_pinned": False}),
            "sbom-wheel": lambda value: value["attestations"]["sbom"].update(
                {"wheel_sha256": self.digest("foreign-wheel")}
            ),
            "signature-registry": lambda value: value["attestations"][
                "signature"
            ].update({"registry_identity_sha256": self.digest("foreign-registry")}),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case), self.assertRaises(CatalogError):
                self.helper_admission(record, mutate=mutate)

    def test_ngc_materialization_rejects_eso_and_secret_observation_substitution(self) -> None:
        mutations = {
            "ESO delivery": lambda value: value.update(
                {"delivery_mode": "external-secrets-nebius-mysterybox"}
            ),
            "invented ESO eligibility": lambda value: value.update(
                {"optional_backend_eligibility_receipt": hashlib.sha256(b"fake").hexdigest()}
            ),
            "Secret value read": lambda value: value["server_observation"].update(
                {"values_recorded": True}
            ),
            "observer method drift": lambda value: value["server_observation"].update(
                {"method": "helm-values"}
            ),
            "placeholder API server": lambda value: value["server_observation"].update(
                {"api_server_identity_sha256": "0" * 64}
            ),
            "Secret name substitution": lambda value: value["secrets"][0].update(
                {"name": "attacker-secret"}
            ),
            "Secret UID substitution": lambda value: value["secrets"][0].update(
                {"uid": "ffffffff-ffff-ffff-ffff-ffffffffffff"}
            ),
            "Secret resourceVersion substitution": lambda value: value["secrets"][0].update(
                {"resource_version": "999"}
            ),
            "Secret type substitution": lambda value: value["secrets"][0].update(
                {"secret_type": "Opaque"}
            ),
            "Secret key-set substitution": lambda value: value["secrets"][1].update(
                {"data_keys": ["token"]}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                observation = copy.deepcopy(self.observation)
                materialization = observation["ngc_credential_materialization"]
                mutate(materialization)
                if label in {"Secret value read", "observer method drift"}:
                    materialization["server_observation_sha256"] = hashlib.sha256(
                        canonical_bytes(materialization["server_observation"])
                    ).hexdigest()
                with self.assertRaises(CatalogError):
                    bind_runtime_prerequisites(self.catalog, observation)

    def test_ngc_materialization_server_digest_binds_observer_and_api_server(self) -> None:
        observation = copy.deepcopy(self.observation)
        observation["ngc_credential_materialization"]["server_observation"][
            "observer_principal_sha256"
        ] = hashlib.sha256(b"foreign-observer").hexdigest()
        with self.assertRaisesRegex(CatalogError, "server observation digest"):
            bind_runtime_prerequisites(self.catalog, observation)

    def capability(
        self,
        record,
        *,
        storage_mode: str = "sfs-pvc",
        mechanisms: list[str] | None = None,
        pool: str | None = None,
    ):
        value = record.to_dict()
        if pool is None:
            placement = value["resources"]["gpu"]["placement"]
            pool = (
                placement["pool"]
                if placement is not None
                else (
                    "b300-burst-8x"
                    if storage_mode == "local-nvme"
                    else "b300-burst-1x"
                )
            )
        node_count = 8 if pool.endswith("8x") else 1
        capacity_type = "regular" if pool == "b300-hot-8x" else "preemptible"
        selector = {
            "capacity.fs2.nebius/gpu-count": str(node_count),
            "capacity.fs2.nebius/pool": "hot" if pool == "b300-hot-8x" else "burst",
            "capacity.fs2.nebius/preset": "b300-8x" if node_count == 8 else "b300-1x",
            "capacity.fs2.nebius/type": capacity_type,
            "workload.fs2.nebius/gpu": "true",
        }
        if storage_mode == "local-nvme":
            selector["kubernetes.io/hostname"] = "fs2-b300-unit-node"
        local_pv_pvc = None
        provider_block_pvc = None
        if storage_mode == "local-nvme":
            local_pv_pvc = {
                "schema": "fs2-serve.nebius.ai/local-pv-pvc-lifecycle/v1",
                "state": "reviewed-implemented",
                "lifecycle_receipt_digest": self.digest("local-pv-pvc-lifecycle"),
                "cache_namespace": "fs2-models",
                "localizer_security_profile": "restricted-unprivileged",
                "storage_class_name": "fs2-local-nvme",
                "volume_binding_mode": "WaitForFirstConsumer",
                "activation_generation": 1,
                "persistent_volume": {
                    "name": "qwen3-8b-local-pv-g1",
                    "uid": "77777777-7777-7777-7777-777777777777",
                    "resource_version": "1",
                    "node_affinity": {
                        "node_name": "fs2-b300-unit-node",
                        "node_uid": "99999999-9999-9999-9999-999999999999",
                        "required_node_selector": dict(sorted(selector.items())),
                    },
                },
                "persistent_volume_claim": {
                    "namespace": "fs2-models",
                    "name": "qwen3-8b-local-g1",
                    "uid": "88888888-8888-8888-8888-888888888888",
                    "resource_version": "1",
                    "volume_name": "qwen3-8b-local-pv-g1",
                    "access_modes": ["ReadWriteOnce"],
                },
                "activation_target": {
                    "api_version": "apps/v1",
                    "kind": "Deployment",
                    "namespace": "fs2-models",
                    "name": record.model_id,
                    "uid": "66666666-6666-6666-6666-666666666666",
                },
                "fencing": {
                    "preemption": "pod-pvc-pv-node-uid",
                    "lost_node": "invalidate-and-recreate-next-activation-generation",
                    "activation_generation_recreation": True,
                },
            }
        if storage_mode == "provider-block-pvc":
            provider_block_pvc = {
                "schema": "fs2-serve.nebius.ai/provider-block-pvc-lifecycle/v2",
                "state": "verified",
                "lifecycle_receipt_digest": self.digest(
                    "provider-block-pvc-lifecycle"
                ),
                "storage_class": {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {
                        "name": "fs2-network-ssd-retain",
                        "uid": "55555555-5555-5555-5555-555555555555",
                        "resourceVersion": "1",
                    },
                    "spec": {
                        "provisioner": "compute.csi.nebius.com",
                        "reclaimPolicy": "Retain",
                        "volumeBindingMode": "WaitForFirstConsumer",
                        "allowVolumeExpansion": True,
                        "parameters": {
                            "type": "NETWORK_SSD",
                            "csi.storage.k8s.io/fstype": "ext4",
                        },
                    },
                },
                "claim": {
                    "namespace": "fs2-models",
                    "name": "qwen3-8b-weights",
                    "uid": "44444444-4444-4444-4444-444444444444",
                    "resource_version": "1",
                    "volume_name": "pvc-44444444-4444-4444-4444-444444444444",
                    "capacity_bytes": 68_719_476_736,
                    "access_modes": ["ReadWriteOnce"],
                    "volume_mode": "Filesystem",
                    "fs_type": "ext4",
                },
            }
        storage = {
            "mode": storage_mode,
            "pvc_requirement_id": (
                None
                if storage_mode in {"local-nvme", "provider-block-pvc"}
                else "fs2-models/shared-cache-pvc"
            ),
            "mount_path": (
                value["cache"]["local_path"]
                if storage_mode == "local-nvme"
                else "/mnt/fs2-provider-block"
                if storage_mode == "provider-block-pvc"
                else "/mnt/fs2-serve-cache"
            ),
            "node_identity": (
                {
                    "name": "fs2-b300-unit-node",
                    "uid": "99999999-9999-9999-9999-999999999999",
                    "provider_id_sha256": self.digest("fs2-b300-unit-provider"),
                }
                if storage_mode == "local-nvme"
                else None
            ),
            "provider_block_pvc": provider_block_pvc,
            "local_pv_pvc": local_pv_pvc,
        }
        nim_image = None
        if value["runtime"]["kind"] == "nim":
            nim_image = {
                "repository": value["model"]["source"]["repository"],
                "tag": "test-exact-tag",
                "expected_digest": value["runtime"]["image"]["digest"],
                "tag_binding_receipt_digest": self.digest(
                    record.model_id + "-tag-binding"
                ),
            }
        raw = {
            "schema": "fs2-serve.nebius.ai/backend-capability/v6",
            "backend_id": "rene-us-north-b300",
            "backend_class": "local-kubernetes",
            "region": "us-north1",
            "admission_scope": "experiment-only",
            "model_id": record.model_id,
            "model_digest": record.digest,
            "model_revision": value["model"]["source"]["revision"],
            "runtime_image_digest": value["runtime"]["image"]["digest"],
            "gpu": {
                "class": "NVIDIA-B300-SXM6-288GB",
                "node_preset": "b300-8x" if node_count == 8 else "b300-1x",
                "node_count": node_count,
                "node_topology": "eight-gpu-nvlink" if node_count == 8 else "single-gpu",
                "workload_count": value["resources"]["gpu"]["count"],
                "workload_topology": value["resources"]["gpu"]["topology"],
            },
            "allowed_mechanisms": mechanisms or ["conventional"],
            "scheduling": {
                "pool": pool,
                "capacity_type": capacity_type,
                "node_selector": dict(sorted(selector.items())),
                "tolerations": [GPU_TOLERATION],
            },
            "storage": storage,
            "runtime_tuple_digest": self.digest(record.model_id + "-runtime-tuple"),
            "backend_identity_digest": self.digest(record.model_id + "-backend"),
            "nim_image": nim_image,
        }
        return bind_backend_capability(record, raw)

    def test_actual_localqueue_is_v1beta2_namespaced_and_matches_the_renderer(self) -> None:
        from jsonschema import Draft202012Validator

        expected = json.loads((CATALOG_ROOT / "kubernetes" / "localqueues.json").read_text())
        self.assertEqual(expected, render_local_queues())
        schema = json.loads(
            (CATALOG_ROOT / "schema" / "localqueues.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(expected)
        self.assertEqual(1, len(expected["items"]))
        for item in expected["items"]:
            self.assertEqual("LocalQueue", item["kind"])
            self.assertEqual("kueue.x-k8s.io/v1beta2", item["apiVersion"])
            self.assertEqual("fs2-models", item["metadata"]["namespace"])
            self.assertEqual("fs2-b300-async", item["spec"]["clusterQueue"])
        with self.assertRaisesRegex(CatalogError, "deployed model-lane"):
            render_local_queue("fs2-faststart")
        self.assertNotIn("ClusterQueue", json.dumps(expected))
        self.assertNotIn("ResourceFlavor", json.dumps(expected))
        self.assertNotIn("AdmissionCheck", json.dumps(expected))

    def test_all_async_job_kinds_are_suspended_kueue_jobs(self) -> None:
        record = self.catalog.model("nv-reason-cxr-3b")
        capability = self.capability(record, storage_mode="sfs-pvc")
        image = "registry.invalid/worker@" + self.digest("worker", image=True)
        for kind in sorted(ASYNC_JOB_KINDS):
            with self.subTest(kind=kind):
                if kind in {"donor", "snapshot"}:
                    with self.assertRaisesRegex(CatalogError, "gated-unimplemented"):
                        self.capability(
                            record,
                            storage_mode="local-nvme",
                            mechanisms=["conventional", "snapshot"],
                        )
                    continue
                job = render_async_job(
                    record,
                    prerequisites=self.prerequisites,
                    job_kind=kind,
                    operation_id="attempt-1",
                    image=image,
                    command=["/runner"],
                    image_pull_requirement_id=(
                        "fs2-faststart/runtime-registry-secret"
                        if kind in {"donor", "snapshot"}
                        else "fs2-models/runtime-registry-secret"
                    ),
                    backend_capability=(None if kind == "cache" else capability),
                )
                self.assertEqual("Job", job["kind"])
                self.assertTrue(job["spec"]["suspend"])
                self.assertIn("kueue.x-k8s.io/queue-name", job["metadata"]["labels"])
                self.assertNotIn("ClusterQueue", json.dumps(job))
                if kind != "cache":
                    self.assertEqual(
                        [GPU_TOLERATION], job["spec"]["template"]["spec"]["tolerations"]
                    )
                    self.assertEqual(
                        capability.runtime_tuple_digest,
                        job["metadata"]["annotations"][
                            "fs2-serve.nebius.ai/runtime-tuple-digest"
                        ],
                    )
                    self.assertEqual(
                        capability.to_dict()["backend_identity_digest"],
                        job["spec"]["template"]["metadata"]["annotations"][
                            "fs2-serve.nebius.ai/backend-identity-digest"
                        ],
                    )

    def test_multi_gpu_criu_and_mutable_images_are_refused(self) -> None:
        glm = self.catalog.model("glm-5-2-fp8")
        with self.assertRaisesRegex(CatalogError, "gated-unimplemented"):
            self.capability(
                glm,
                storage_mode="local-nvme",
                mechanisms=["conventional", "snapshot"],
            )
        with self.assertRaisesRegex(CatalogError, "immutable"):
            render_async_job(
                glm,
                prerequisites=self.prerequisites,
                job_kind="evaluation",
                operation_id="attempt-1",
                image="registry.invalid/worker:latest",
                command=["/runner"],
                image_pull_requirement_id="fs2-models/runtime-registry-secret",
            )

    def test_prepull_and_local_pv_pvc_gate_bind_selector_digest_and_owner(self) -> None:
        cxr = self.catalog.model("nv-reason-cxr-3b")
        sfs_capability = self.capability(cxr)
        prepull = render_image_prepull_job(
            cxr,
            prerequisites=self.prerequisites,
            operation_id="prepull-1",
            backend_capability=sfs_capability,
        )
        self.assertEqual(
            sfs_capability.node_selector,
            prepull["spec"]["template"]["spec"]["nodeSelector"],
        )
        self.assertEqual(
            [GPU_TOLERATION], prepull["spec"]["template"]["spec"]["tolerations"]
        )
        for metadata in (
            prepull["metadata"],
            prepull["spec"]["template"]["metadata"],
        ):
            self.assertEqual(
                sfs_capability.runtime_tuple_digest,
                metadata["annotations"]["fs2-serve.nebius.ai/runtime-tuple-digest"],
            )
            self.assertEqual(
                sfs_capability.to_dict()["backend_identity_digest"],
                metadata["annotations"][
                    "fs2-serve.nebius.ai/backend-identity-digest"
                ],
            )
        with self.assertRaisesRegex(CatalogError, "gated-unimplemented"):
            self.capability(cxr, storage_mode="local-nvme")
        renderer_sources = "\n".join(
            (
                (CATALOG_ROOT / "fs2_serve_catalog" / "kubernetes.py").read_text(),
                (CATALOG_ROOT / "fs2_serve_catalog" / "workloads.py").read_text(),
            )
        )
        self.assertNotIn("DirectoryOrCreate", renderer_sources)
        self.assertNotIn('"hostPath"', renderer_sources)
        with self.assertRaisesRegex(CatalogError, "NIM Operator-owned"):
            render_localization_job(
                self.catalog.model("boltz2"),
                prerequisites=self.prerequisites,
                operation_id="stage-1",
                localizer_image="registry.invalid/localizer@" + self.digest("localizer", image=True),
                artifact_manifest_digest=self.digest("artifact-manifest"),
                artifact_content_digest=self.digest("artifact-content"),
                backend_capability=self.capability(
                    self.catalog.model("boltz2"), storage_mode="nimcache-pvc"
                ),
            )
        with self.assertRaisesRegex(CatalogError, "storage mode"):
            render_localization_job(
                cxr,
                prerequisites=self.prerequisites,
                operation_id="stage-2",
                localizer_image="registry.invalid/localizer@"
                + self.digest("localizer", image=True),
                artifact_manifest_digest=self.digest("artifact-manifest"),
                artifact_content_digest=self.digest("artifact-content"),
                backend_capability=sfs_capability,
            )

    def test_native_kserve_and_nimcache_adapters_preserve_boundaries(self) -> None:
        cxr = self.catalog.model("nv-reason-cxr-3b")
        capability = self.capability(cxr)
        uri = "sfs://fs2-cache/mnt/fs2-serve-cache/models/nv-reason-cxr-3b/sha256/" + self.digest("cxr-content")
        native = render_native_http_workload(
            cxr,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            artifact_uri=uri,
            backend_capability=capability,
        )
        self.assertEqual(
            ["Deployment", "Service", "NetworkPolicy"],
            [item["kind"] for item in native["items"]],
        )
        native_pod = native["items"][0]["spec"]["template"]["spec"]
        native_deployment = native["items"][0]
        self.assertEqual(0, native_deployment["spec"]["replicas"])
        self.assertEqual(
            "fs2-model-activation-controller",
            native_deployment["metadata"]["annotations"][
                "fs2-serve.nebius.ai/replica-field-owner"
            ],
        )
        self.assertEqual(
            "RespectIgnoreDifferences=true",
            native_deployment["metadata"]["annotations"][
                "argocd.argoproj.io/sync-options"
            ],
        )
        self.assertNotIn("hostPath", json.dumps(native_pod))
        self.assertEqual([GPU_TOLERATION], native_pod["tolerations"])
        offline_env = {
            item["name"]: item["value"]
            for item in native_pod["containers"][0]["env"]
            if item["name"].endswith("OFFLINE")
        }
        self.assertEqual(
            {
                "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            offline_env,
        )
        self.assertEqual([], native["items"][2]["spec"]["egress"])
        self.assertEqual(["Egress"], native["items"][2]["spec"]["policyTypes"])
        kserve = render_kserve_standard_workload(
            cxr,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            artifact_uri=uri,
            backend_capability=capability,
        )
        self.assertEqual("Standard", kserve["metadata"]["annotations"]["serving.kserve.io/deploymentMode"])
        predictor = kserve["spec"]["predictor"]
        cxr_container = predictor["containers"][0]
        self.assertNotIn("command", cxr_container)
        self.assertEqual("/mnt/fs2-serve-cache/models/nv-reason-cxr-3b/sha256/" + self.digest("cxr-content"), cxr_container["args"][1])
        self.assertNotIn("nvidia/NV-Reason-CXR-3B", cxr_container["args"])
        self.assertEqual("nv-reason-cxr-3b", cxr_container["args"][3])
        self.assertTrue(predictor["securityContext"]["runAsNonRoot"])
        self.assertTrue(cxr_container["securityContext"]["allowPrivilegeEscalation"] is False)
        boltz = self.catalog.model("boltz2")
        nim_capability = self.capability(boltz, storage_mode="nimcache-pvc")
        cache = render_nim_operator_cache(
            boltz,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            backend_capability=nim_capability,
        )
        self.assertFalse(cache["spec"]["storage"]["pvc"]["create"])
        self.assertEqual([GPU_TOLERATION], cache["spec"]["tolerations"])
        self.assertEqual(
            nim_capability.runtime_tuple_digest,
            cache["metadata"]["annotations"][
                "fs2-serve.nebius.ai/runtime-tuple-digest"
            ],
        )
        self.assertEqual(
            "nim-operator-nimcache",
            cache["metadata"]["annotations"]["fs2-serve.nebius.ai/cache-owner"],
        )
        service = render_nim_operator_service(
            boltz,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            backend_capability=nim_capability,
        )
        self.assertEqual("NIMService", service["kind"])
        self.assertEqual(0, service["spec"]["replicas"])
        self.assertEqual(
            "fs2-model-activation-controller",
            service["metadata"]["annotations"][
                "fs2-serve.nebius.ai/replica-field-owner"
            ],
        )
        self.assertEqual("Always", service["spec"]["image"]["pullPolicy"])
        self.assertEqual([GPU_TOLERATION], service["spec"]["tolerations"])
        self.assertIn("disabled-pending-pod-imageid", json.dumps(service))
        self.assertNotIn("hostPath", json.dumps(service))
        with self.assertRaisesRegex(CatalogError, "cache identity differs"):
            render_nim_operator_service(
                boltz,
                prerequisites=self.prerequisites,
                namespace="fs2-models",
                backend_capability=nim_capability,
                nim_cache_name="substituted-cache",
            )
        with self.assertRaisesRegex(CatalogError, "NIM runtimes must use"):
            render_native_http_workload(
                boltz,
                prerequisites=self.prerequisites,
                namespace="fs2-models",
                artifact_uri=uri.replace("nv-reason-cxr-3b", "boltz2"),
                backend_capability=nim_capability,
            )

    def test_replica_field_contract_is_zero_bootstrap_and_activation_owned(self) -> None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(
            json.loads(
                (CATALOG_ROOT / "schema" / "replica-field-ownership.schema.json").read_text()
            )
        )
        for api_version, kind in (
            ("apps/v1", "Deployment"),
            ("apps.nvidia.com/v1alpha1", "NIMService"),
        ):
            with self.subTest(kind=kind):
                contract = replica_field_ownership(api_version, kind)
                validator.validate(contract)
                self.assertEqual(0, contract["bootstrap_value"])
                self.assertEqual("/spec/replicas", contract["field"])
                self.assertEqual(
                    "fs2-model-activation-controller",
                    contract["replica_scaler_owner"],
                )
                self.assertEqual(
                    ["/spec/replicas"],
                    contract["gitops"]["ignore_differences_json_pointers"],
                )
                self.assertTrue(contract["gitops"]["respect_ignore_differences"])
                self.assertEqual(
                    "forbidden", contract["gitops"]["force_apply_conflicts"]
                )
        with self.assertRaisesRegex(CatalogError, "activation targets"):
            replica_field_ownership("serving.kserve.io/v1beta1", "InferenceService")

    def test_acquisition_and_ngc_canary_are_explicit_and_value_suppressed(self) -> None:
        cxr = self.catalog.model("nv-reason-cxr-3b")
        acquisition = render_artifact_acquisition_job(
            cxr,
            self.catalog.acquisition_plan("nv-reason-cxr-3b"),
            prerequisites=self.prerequisites,
            operation_id="acquire-1",
            helper_image_admission=self.helper_admission(cxr),
        )
        pod = acquisition["spec"]["template"]["spec"]
        helper_admission = self.helper_admission(cxr)
        self.assertEqual(
            helper_admission.image_reference,
            pod["containers"][0]["image"],
        )
        environment = {item["name"]: item for item in pod["containers"][0]["env"]}
        self.assertEqual(
            "metadata.uid",
            environment["FS2_ACQUISITION_POD_UID"]["valueFrom"]["fieldRef"][
                "fieldPath"
            ],
        )
        self.assertEqual(
            "metadata.annotations['fs2-serve.nebius.ai/job-uid']",
            environment["FS2_ACQUISITION_JOB_UID"]["valueFrom"]["fieldRef"][
                "fieldPath"
            ],
        )
        self.assertTrue(acquisition["spec"]["suspend"])
        self.assertEqual(
            "patch-server-observed-uid-before-unsuspend",
            acquisition["metadata"]["annotations"][
                "fs2-serve.nebius.ai/job-uid-gate"
            ],
        )
        self.assertEqual("fs2-cache", pod["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(10001, pod["securityContext"]["runAsUser"])
        self.assertEqual(10001, pod["securityContext"]["runAsGroup"])
        self.assertEqual(10001, pod["securityContext"]["fsGroup"])
        self.assertEqual("OnRootMismatch", pod["securityContext"]["fsGroupChangePolicy"])
        self.assertEqual("Strict", pod["securityContext"]["supplementalGroupsPolicy"])
        self.assertEqual(10001, pod["containers"][0]["securityContext"]["runAsUser"])
        self.assertNotIn("NGC_API_KEY", json.dumps(acquisition))

        with self.assertRaises(TypeError):
            render_artifact_acquisition_job(
                cxr,
                self.catalog.acquisition_plan("nv-reason-cxr-3b"),
                prerequisites=self.prerequisites,
                operation_id="acquire-1",
                acquisition_image="registry.invalid/caller@" + self.digest("caller", image=True),
            )
        with self.assertRaisesRegex(CatalogError, "model plan"):
            render_artifact_acquisition_job(
                cxr,
                self.catalog.acquisition_plan("nv-reason-cxr-3b"),
                prerequisites=self.prerequisites,
                operation_id="acquire-1",
                helper_image_admission=self.helper_admission(
                    self.catalog.model("qwen3-8b")
                ),
            )
        with self.assertRaisesRegex(CatalogError, "model plan"):
            render_artifact_acquisition_job(
                cxr,
                self.catalog.acquisition_plan("nv-reason-cxr-3b"),
                prerequisites=self.prerequisites,
                operation_id="acquire-1",
                helper_image_admission=replace(
                    helper_admission,
                    image_reference="registry.invalid/foreign/helper@"
                    + helper_admission.image_digest,
                ),
            )

        qwen = self.catalog.model("qwen3-8b")
        storage_admission, writer_admission = self.provider_admissions(qwen)
        claim = render_provider_block_pvc(
            qwen, storage_class_admission=storage_admission
        )
        self.assertEqual("qwen3-8b-weights", claim["metadata"]["name"])
        self.assertEqual("fs2-network-ssd-retain", claim["spec"]["storageClassName"])
        self.assertEqual(["ReadWriteOnce"], claim["spec"]["accessModes"])
        self.assertEqual("Filesystem", claim["spec"]["volumeMode"])
        self.assertEqual("64Gi", claim["spec"]["resources"]["requests"]["storage"])
        self.assertEqual("keep", claim["metadata"]["annotations"]["helm.sh/resource-policy"])
        self.assertEqual(
            storage_admission.receipt_digest,
            claim["metadata"]["annotations"][
                "fs2-serve.nebius.ai/protected-storage-class-receipt-digest"
            ],
        )
        with self.assertRaises(TypeError):
            render_provider_block_pvc(qwen)
        qwen_acquisition = render_artifact_acquisition_job(
            qwen,
            self.catalog.acquisition_plan("qwen3-8b"),
            prerequisites=self.prerequisites,
            operation_id="qwen-acquire-1",
            helper_image_admission=self.helper_admission(qwen),
            storage_class_admission=storage_admission,
            writer_admission=writer_admission,
        )
        qwen_pod = qwen_acquisition["spec"]["template"]["spec"]
        self.assertEqual(
            qwen.to_dict()["resources"]["gpu"]["placement"]["node_selector"],
            qwen_pod["nodeSelector"],
        )
        self.assertEqual([GPU_TOLERATION], qwen_pod["tolerations"])
        self.assertEqual(10001, qwen_pod["securityContext"]["runAsUser"])
        self.assertEqual("ext4", qwen_acquisition["metadata"]["annotations"]["fs2-serve.nebius.ai/required-filesystem"])
        self.assertEqual(
            "exclusive-create-write-fsync-read-unlink",
            qwen_acquisition["metadata"]["annotations"][
                "fs2-serve.nebius.ai/fresh-write-proof"
            ],
        )
        self.assertEqual(
            "qwen3-8b-weights",
            qwen_pod["volumes"][0]["persistentVolumeClaim"]["claimName"],
        )
        requests = qwen_pod["containers"][0]["resources"]["requests"]
        self.assertNotIn("nvidia.com/gpu", requests)
        self.assertEqual(
            "true",
            qwen_acquisition["metadata"]["annotations"][
                "fs2-serve.nebius.ai/sole-writer"
            ],
        )
        self.assertEqual(
            writer_admission.lease_uid,
            qwen_acquisition["metadata"]["annotations"][
                "fs2-serve.nebius.ai/writer-lease-uid"
            ],
        )
        with self.assertRaisesRegex(CatalogError, "signed StorageClass and writer"):
            render_artifact_acquisition_job(
                qwen,
                self.catalog.acquisition_plan("qwen3-8b"),
                prerequisites=self.prerequisites,
                operation_id="qwen-acquire-1",
                helper_image_admission=self.helper_admission(qwen),
            )

        resolved_qwen = self.resolved_qwen()
        provider_capability = self.capability(
            resolved_qwen, storage_mode="provider-block-pvc"
        )
        uri = (
            "pvc://fs2-models/qwen3-8b-weights/models/qwen3-8b/sha256/"
            + self.digest("qwen-content")
        )
        qwen_workload = render_native_http_workload(
            resolved_qwen,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            artifact_uri=uri,
            backend_capability=provider_capability,
        )
        qwen_runtime_pod = qwen_workload["items"][0]["spec"]["template"]["spec"]
        self.assertEqual(
            {"claimName": "qwen3-8b-weights", "readOnly": True},
            qwen_runtime_pod["volumes"][0]["persistentVolumeClaim"],
        )
        self.assertTrue(qwen_runtime_pod["containers"][0]["volumeMounts"][0]["readOnly"])
        self.assertEqual(
            1,
            qwen_runtime_pod["containers"][0]["resources"]["requests"][
                "nvidia.com/gpu"
            ],
        )
        qwen_command = qwen_runtime_pod["containers"][0]["command"]
        self.assertEqual(
            "/mnt/fs2-provider-block/models/qwen3-8b/sha256/"
            + self.digest("qwen-content"),
            qwen_command[2],
        )
        self.assertEqual("qwen3-8b", qwen_command[4])
        self.assertNotIn("Qwen/Qwen3-8B", qwen_command)

        glm = self.catalog.model("glm-5-2-fp8")
        glm_capability = self.capability(
            glm, storage_mode="sfs-pvc", pool="b300-hot-8x"
        )
        glm_uri = (
            "sfs://fs2-cache/mnt/fs2-serve-cache/models/glm-5-2-fp8/sha256/"
            + self.digest("glm-content")
        )
        glm_workload = render_native_http_workload(
            glm,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            artifact_uri=glm_uri,
            backend_capability=glm_capability,
        )
        glm_command = glm_workload["items"][0]["spec"]["template"]["spec"][
            "containers"
        ][0]["command"]
        self.assertEqual(
            "/mnt/fs2-serve-cache/models/glm-5-2-fp8/sha256/"
            + self.digest("glm-content"),
            glm_command[2],
        )
        self.assertEqual("glm-5-2-fp8", glm_command[4])
        self.assertNotIn("zai-org/GLM-5.2-FP8", glm_command)

        boltz = self.catalog.model("boltz2")
        capability = self.capability(boltz, storage_mode="nimcache-pvc")
        canary = render_ngc_target_node_canary_job(
            boltz,
            self.catalog.acquisition_plan("boltz2"),
            prerequisites=self.prerequisites,
            operation_id="canary-1",
            backend_capability=capability,
        )
        canary_pod = canary["spec"]["template"]["spec"]
        self.assertEqual([{"name": "fs2-ngc-pull"}], canary_pod["imagePullSecrets"])
        self.assertEqual([GPU_TOLERATION], canary_pod["tolerations"])
        self.assertEqual(
            capability.runtime_tuple_digest,
            canary["metadata"]["annotations"][
                "fs2-serve.nebius.ai/runtime-tuple-digest"
            ],
        )
        self.assertEqual(
            capability.to_dict()["backend_identity_digest"],
            canary["spec"]["template"]["metadata"]["annotations"][
                "fs2-serve.nebius.ai/backend-identity-digest"
            ],
        )
        self.assertIn("secretKeyRef", json.dumps(canary))
        rendered_canary = json.dumps(canary)
        self.assertNotIn('"kind": "Secret"', rendered_canary)
        self.assertNotIn('"kind": "ExternalSecret"', rendered_canary)
        self.assertNotIn('"kind": "SecretStore"', rendered_canary)
        self.assertNotIn('"data":', rendered_canary)
        self.assertNotIn('"stringData":', rendered_canary)
        ngc_env = next(
            item
            for item in canary_pod["containers"][0]["env"]
            if item["name"] == "NGC_API_KEY"
        )
        self.assertEqual({"valueFrom"}, set(ngc_env) - {"name"})
        with self.assertRaisesRegex(CatalogError, "SM103-incompatible"):
            self.capability(
                self.catalog.model("evo2-40b"), storage_mode="nimcache-pvc"
            )

    def test_prerequisite_binding_requires_fresh_precreated_ngc_observation(self) -> None:
        leaked = json.loads(json.dumps(self.observation))
        leaked["resources"][0]["data"] = {"credential": "forbidden"}
        with self.assertRaisesRegex(CatalogError, "keys differ"):
            bind_runtime_prerequisites(self.catalog, leaked)
        legacy = json.loads(json.dumps(self.observation))
        legacy["legacy_phase_7c_hmac_reused"] = True
        with self.assertRaisesRegex(CatalogError, "Phase-7c HMAC reuse is forbidden"):
            bind_runtime_prerequisites(self.catalog, legacy)
        for field, message in (
            ("legacy_ngc_secret_copied", "legacy NGC Secret copying is forbidden"),
            (
                "legacy_plaintext_rotation_source_used",
                "legacy plaintext rotation sources are forbidden",
            ),
            ("exposed_evo_bearer_reused", "exposed Evo bearer reuse is forbidden"),
        ):
            with self.subTest(field=field):
                candidate = json.loads(json.dumps(self.observation))
                candidate[field] = True
                with self.assertRaisesRegex(CatalogError, message):
                    bind_runtime_prerequisites(self.catalog, candidate)
        missing = json.loads(json.dumps(self.observation))
        missing["ngc_credential_materialization"] = None
        with self.assertRaisesRegex(CatalogError, "NGC credential materialization"):
            bind_runtime_prerequisites(self.catalog, missing)
        copied = json.loads(json.dumps(self.observation))
        copied["ngc_credential_materialization"]["legacy_ngc_secret_copied"] = True
        with self.assertRaisesRegex(CatalogError, "not fresh and platform-owned"):
            bind_runtime_prerequisites(self.catalog, copied)
        compromised = json.loads(json.dumps(self.observation))
        compromised["ngc_credential_materialization"][
            "compromise_review_status"
        ] = "unknown"
        with self.assertRaisesRegex(CatalogError, "not fresh and platform-owned"):
            bind_runtime_prerequisites(self.catalog, compromised)
        fake_issuance = json.loads(json.dumps(self.observation))
        fake_issuance["ngc_credential_materialization"][
            "issuer_receipt_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(CatalogError, "placeholder"):
            bind_runtime_prerequisites(self.catalog, fake_issuance)
        substituted = json.loads(json.dumps(self.observation))
        substituted["ngc_credential_materialization"]["secrets"][0]["uid"] = (
            "ffffffff-ffff-ffff-ffff-ffffffffffff"
        )
        with self.assertRaisesRegex(CatalogError, "identity differs"):
            bind_runtime_prerequisites(self.catalog, substituted)

    def test_public_workloads_do_not_invent_registry_secrets_and_sm103_fails_closed(self) -> None:
        cxr = self.catalog.model("nv-reason-cxr-3b")
        capability = self.capability(cxr, storage_mode="sfs-pvc")
        uri = (
            "sfs://fs2-cache/mnt/fs2-serve-cache/models/nv-reason-cxr-3b/sha256/"
            + self.digest("cxr-content")
        )
        workload = render_native_http_workload(
            cxr,
            prerequisites=self.prerequisites,
            namespace="fs2-models",
            artifact_uri=uri,
            backend_capability=capability,
        )
        pod = workload["items"][0]["spec"]["template"]["spec"]
        self.assertNotIn("imagePullSecrets", pod)
        self.assertNotIn("hostPath", json.dumps(pod))
        with self.assertRaisesRegex(CatalogError, "SM103-incompatible"):
            self.capability(
                self.catalog.model("evo2-40b"), storage_mode="sfs-pvc"
            )

    def test_backend_capability_rejects_scheduling_storage_and_identity_adversaries(self) -> None:
        record = self.catalog.model("nv-reason-cxr-3b")
        good = self.capability(record).to_dict()
        adversaries = {
            "missing-toleration": lambda value: value["scheduling"].update(
                {"tolerations": []}
            ),
            "gpu-substitution": lambda value: value["gpu"].update(
                {"class": "NVIDIA-H200-SXM"}
            ),
            "model-substitution": lambda value: value.update(
                {"model_digest": self.digest("other-model")}
            ),
            "dummy-receipt": lambda value: value.update(
                {"backend_identity_digest": "a" * 64}
            ),
            "nvme-on-1x": lambda value: value["storage"].update(
                {
                    "mode": "local-nvme",
                    "pvc_requirement_id": None,
                    "mount_path": record.to_dict()["cache"]["local_path"],
                    "node_identity": good["storage"]["node_identity"],
                    "local_pv_pvc": None,
                }
            ),
        }
        for label, mutate in adversaries.items():
            with self.subTest(label=label):
                candidate = json.loads(json.dumps(good))
                mutate(candidate)
                with self.assertRaises(CatalogError):
                    bind_backend_capability(record, candidate)

    def test_backend_capability_schema_and_fixture(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover
            self.fail(f"jsonschema is required for backend capability validation: {exc}")
        schema = json.loads(
            (CATALOG_ROOT / "schema" / "backend-capability.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(
            self.capability(self.catalog.model("nv-reason-cxr-3b")).to_dict()
        )

    def test_qwen_backend_separates_one_gpu_allocation_from_eight_gpu_node(self) -> None:
        record = self.catalog.model("qwen3-8b")
        provider = self.capability(record, storage_mode="provider-block-pvc")
        self.assertEqual(1, provider.workload_gpu_count)
        self.assertEqual(8, provider.node_gpu_count)
        self.assertEqual("b300-burst-8x", provider.to_dict()["scheduling"]["pool"])
        self.assertEqual("8", provider.node_selector["capacity.fs2.nebius/gpu-count"])
        self.assertEqual([GPU_TOLERATION], provider.tolerations)
        self.assertEqual("provider-block-pvc", provider.storage_mode)

        with self.assertRaisesRegex(CatalogError, "gated-unimplemented"):
            self.capability(record, storage_mode="local-nvme")
        with self.assertRaisesRegex(CatalogError, "not admitted"):
            self.capability(record, storage_mode="sfs-pvc")
        with self.assertRaisesRegex(CatalogError, "model node placement"):
            self.capability(
                record, storage_mode="provider-block-pvc", pool="b300-burst-1x"
            )

    def test_qwen_backend_rejects_unobserved_or_drifted_storage_class(self) -> None:
        record = self.catalog.model("qwen3-8b")
        base = self.capability(record, storage_mode="provider-block-pvc").to_dict()
        cases = {
            "default-delete-class": lambda value: value["storage"][
                "provider_block_pvc"
            ]["storage_class"]["spec"].update({"reclaimPolicy": "Delete"}),
            "volume-type-drift": lambda value: value["storage"][
                "provider_block_pvc"
            ]["storage_class"]["spec"]["parameters"].update(
                {"type": "NETWORK_SSD_NON_REPLICATED"}
            ),
            "filesystem-parameter-drift": lambda value: value["storage"][
                "provider_block_pvc"
            ]["storage_class"]["spec"]["parameters"].update(
                {"csi.storage.k8s.io/fstype": "xfs"}
            ),
            "missing-server-uid": lambda value: value["storage"][
                "provider_block_pvc"
            ]["storage_class"]["metadata"].pop("uid"),
            "empty-resource-version": lambda value: value["storage"][
                "provider_block_pvc"
            ]["storage_class"]["metadata"].update({"resourceVersion": ""}),
            "caller-semantic-fields": lambda value: value["storage"][
                "provider_block_pvc"
            ].update(
                {
                    "storage_class": {
                        "name": "fs2-network-ssd-retain",
                        "uid": "55555555-5555-5555-5555-555555555555",
                        "resource_version": "1",
                        "provisioner": "compute.csi.nebius.com",
                        "reclaim_policy": "Retain",
                        "volume_binding_mode": "WaitForFirstConsumer",
                        "allow_volume_expansion": True,
                        "volume_type": "NETWORK_SSD",
                        "fs_type": "ext4",
                    }
                }
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                candidate = copy.deepcopy(base)
                mutate(candidate)
                with self.assertRaises(CatalogError):
                    bind_backend_capability(record, candidate)

    def test_federated_h200_capability_is_exact_but_cannot_render_local_objects(self) -> None:
        record = self.catalog.model("molmim")
        value = record.to_dict()
        inventory = self.catalog.federated_backend("molmim")
        assert inventory is not None
        raw = {
            "schema": "fs2-serve.nebius.ai/backend-capability/v6",
            "backend_id": "federated-kserve-nim",
            "backend_class": "federated-upstream",
            "region": "us-central1",
            "admission_scope": "experiment-only",
            "model_id": record.model_id,
            "model_digest": record.digest,
            "model_revision": value["model"]["source"]["revision"],
            "runtime_image_digest": value["runtime"]["image"]["digest"],
            "gpu": {
                "class": "NVIDIA-H200-SXM",
                "node_preset": "exact-upstream-private",
                "node_count": 1,
                "node_topology": "single-gpu",
                "workload_count": 1,
                "workload_topology": "single-gpu",
            },
            "allowed_mechanisms": ["conventional"],
            "scheduling": None,
            "storage": None,
            "runtime_tuple_digest": None,
            "backend_identity_digest": self.digest("molmim-federated-backend"),
            "nim_image": None,
        }
        capability = bind_backend_capability(
            record, raw, federated_backend=inventory
        )
        self.assertEqual("NVIDIA-H200-SXM", capability.gpu_class)
        with self.assertRaisesRegex(CatalogError, "local backend capability"):
            render_nim_operator_cache(
                record,
                prerequisites=self.prerequisites,
                namespace="fs2-models",
                backend_capability=capability,
            )

    def test_nim_tag_binding_runtime_drift_is_rejected(self) -> None:
        record = self.catalog.model("boltz2")
        raw = self.capability(record, storage_mode="nimcache-pvc").to_dict()
        raw["nim_image"]["expected_digest"] = self.digest(
            "wrong-nim-image", image=True
        )
        with self.assertRaisesRegex(CatalogError, "different image digest"):
            bind_backend_capability(record, raw)


if __name__ == "__main__":
    unittest.main()
