from __future__ import annotations

import hashlib
import json
import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)
from fs2_serve_catalog.consumer import ServingBinding, ServingBindings
from fs2_serve_catalog.loader import CatalogError, load_catalog
from fs2_serve_catalog.variant_promotions import (
    REQUIRED_ATTESTOR_ROLES,
    bind_variant_gateway_catalog,
    load_model_variant_promotions,
    load_variant_gateway_catalog,
    variant_promotion_contract_fixture,
)


CATALOG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CATALOG_ROOT / "packaged-repository"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class VariantPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.role_keys = {
            role: Ed25519PrivateKey.generate() for role in REQUIRED_ATTESTOR_ROLES
        }
        self.private_key = self.role_keys["semantic"]
        self.review_private_key = self.role_keys["review"]
        self.session_id = digest("variant-promotion-unit-session")
        self.validation_time = datetime(2026, 8, 27, 22, 22, 0, tzinfo=timezone.utc)
        self.nonce_index = 0

    def trusted(self) -> dict[str, str]:
        keys = tuple(key.public_key() for key in self.role_keys.values())
        return {public_key_id(public): public_key_value(public) for public in keys}

    def trust_policy(self) -> dict[str, object]:
        return {
            "schema": "fs2-serve.nebius.ai/model-variant-attestor-policy/v1",
            "principals": {
                role: {
                    "role": role,
                    "group": f"fs2-serve-variant-{role}",
                    "enabled": True,
                    "key_id": public_key_id(self.role_keys[role].public_key()),
                    "public_key": public_key_value(self.role_keys[role].public_key()),
                }
                for role in REQUIRED_ATTESTOR_ROLES
            },
            "separation": {
                "unique_key_per_role": True,
                "unique_group_per_role": True,
            },
        }

    def role_for_kind(self, kind: str) -> str:
        return {
            "artifacts": "artifact",
            "variant-supply-objects": "supply-signature",
            "variant-license-artifacts": "license",
            "variant-supplies": "supply",
            "variant-runtime-tuples": "runtime",
            "variant-semantics": "semantic",
            "variant-cohorts": "cohort",
            "variant-cold-boundaries": "cold-boundary",
            "variant-kubernetes-observations": "backend-readiness",
            "variant-backend-readiness": "backend-readiness",
            "variant-preemptions": "preemption",
            "variant-lifecycles": "lifecycle",
            "variant-qualifications": "qualification",
            "variant-reviews": "review",
        }[kind]

    def binding(self, catalog, *, model_id: str = "proteinmpnn") -> ServingBinding:
        record = catalog.model(model_id).to_dict()
        return ServingBinding(
            model_id=model_id,
            binding_digest=digest(f"disabled-serving-binding:{model_id}"),
            model_digest=catalog.model(model_id).digest,
            enabled=True,
            ready=True,
            valid_until="2026-08-27T23:00:00Z",
            execution_mode="http",
            backend_namespace="fs2-models",
            backend_service_name=model_id,
            backend_port=8000,
            service_origin=f"http://{model_id}.fs2-models.svc.cluster.local:8000",
            activation=None,  # type: ignore[arg-type]
            backend_class="local-kubernetes",
            backend_region="us-north1",
            backend_gpu_class="NVIDIA B300",
            backend_runtime_image_digest="sha256:"
            + digest("proteinmpnn-portable-runtime-image"),
            backend_endpoint_identity_sha256=digest(f"backend:{model_id}"),
            backend_trust_bundle_sha256=digest("cluster-trust"),
            backend_credential_requirement_id=None,
            gateway_class="fs2-serve-gateway",
            gateway_namespace="fs2-system",
            gateway_service_name="fs2-serve-control-plane",
            gateway_service_uid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            gateway_port=8080,
            gateway_identity_sha256=digest("gateway-service-identity"),
            gateway_auth_class="scoped-api-key",
            protocols=tuple(record["interface"]["protocols"]),
            endpoints=MappingProxyType(record["interface"]["endpoints"]),
            operations=tuple(record["interface"]["policy"]["operations"]),
            mcp_tool_name=model_id.replace("-", "_"),
            mcp_description="Disabled canonical route skeleton.",
            mcp_enabled=False,
            artifact_manifest_digest=None,
            artifact_uri=None,
            storage_mode=None,
            acquisition_receipt_digest=None,
            prerequisite_receipt_digest=None,
            target_node_canary_digest=None,
            placement_receipt_digest=None,
            runtime_tuple_digest=None,
            prepared_qualification_digest=None,
            new_node_qualification_digest=None,
            semantic_evidence_digest=None,
            readiness_evidence_digest=None,
            backend_evidence_digest=None,
            federated_qualification_digest=None,
            evidence_session_id=None,
        )

    def write_attestation(
        self,
        root: Path,
        *,
        kind: str,
        subject_schema: str,
        subject_digest: str,
        model_id: str,
        claims: dict[str, object],
        private_key: Ed25519PrivateKey | None = None,
        issued_at: str = "2026-08-27T22:20:00Z",
        expires_at: str = "2026-08-27T23:00:00Z",
    ) -> None:
        self.nonce_index += 1
        value = create_signed_attestation(
            private_key=private_key or self.role_keys[self.role_for_kind(kind)],
            session_id=self.session_id,
            nonce=digest(f"variant-attestation-nonce:{self.nonce_index}"),
            issued_at=issued_at,
            expires_at=expires_at,
            kind=kind,
            subject_schema=subject_schema,
            subject_digest=subject_digest,
            model_id=model_id,
            claims=claims,
        )
        directory = root / "attestations" / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{subject_digest}.json").write_text(json.dumps(value) + "\n")

    def write_receipt(
        self,
        root: Path,
        kind: str,
        unsigned: dict[str, object],
        claims: dict[str, object],
        *,
        private_key: Ed25519PrivateKey | None = None,
        issued_at: str = "2026-08-27T22:20:00Z",
    ) -> tuple[str, dict[str, object]]:
        receipt_digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        value = {**unsigned, "receipt_digest": receipt_digest}
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{receipt_digest}.json").write_text(json.dumps(value) + "\n")
        self.write_attestation(
            root,
            kind=kind,
            subject_schema=str(unsigned["schema"]),
            subject_digest=receipt_digest,
            model_id=str(unsigned.get("exposed_model_id", "proteinmpnn")),
            claims=claims,
            private_key=private_key,
            issued_at=issued_at,
        )
        return receipt_digest, value

    def write_raw_object(
        self,
        root: Path,
        kind: str,
        value: dict[str, object],
        claims: dict[str, object],
        *,
        role: str,
        issued_at: str = "2026-08-27T22:20:00Z",
    ) -> str:
        raw = canonical_bytes(value)
        object_digest = hashlib.sha256(raw).hexdigest()
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{object_digest}.json").write_bytes(raw)
        self.write_attestation(
            root,
            kind=kind,
            subject_schema=str(value["schema"]),
            subject_digest=object_digest,
            model_id="proteinmpnn",
            claims=claims,
            private_key=self.role_keys[role],
            issued_at=issued_at,
        )
        return object_digest

    def write_raw_bytes(
        self,
        root: Path,
        kind: str,
        raw: bytes,
        schema: str,
        claims: dict[str, object],
        *,
        role: str,
    ) -> str:
        object_digest = hashlib.sha256(raw).hexdigest()
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{object_digest}.bin").write_bytes(raw)
        self.write_attestation(
            root,
            kind=kind,
            subject_schema=schema,
            subject_digest=object_digest,
            model_id="proteinmpnn",
            claims=claims,
            private_key=self.role_keys[role],
        )
        return object_digest

    def gateway_subject(self, binding: ServingBinding) -> dict[str, object]:
        return {
            "gateway_class": binding.gateway_class,
            "gateway_namespace": binding.gateway_namespace,
            "gateway_service_name": binding.gateway_service_name,
            "gateway_service_uid": binding.gateway_service_uid,
            "gateway_identity_sha256": binding.gateway_identity_sha256,
            "gateway_auth_class": binding.gateway_auth_class,
            "route_id": binding.model_id,
            "backend_namespace": binding.backend_namespace,
            "backend_service_name": binding.backend_service_name,
            "backend_service_uid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "backend_port": binding.backend_port,
            "transport": "gateway-proxy",
        }

    def build_fixture(
        self,
        *,
        cold_attempts: int = 3,
        warm_attempts: int = 10,
        runtime_gpu_architecture: str = "sm_103",
        kernel_architecture: str = "sm_103",
        oci_subject_override: str | None = None,
        license_id: str | None = None,
        replay_semantic: bool = False,
        independent_review: bool = True,
        runtime_revision: str | None = None,
        supply_repository: str | None = None,
        supply_revision_url: str | None = None,
        supply_file_count_delta: int = 0,
        supply_bytes_delta: int = 0,
        cold_failures_override: int | None = None,
        warm_failures_override: int | None = None,
        overlap_attempts: bool = False,
        duplicate_cross_cohort_attempt_id: bool = False,
        preemption_cross_pair: bool = False,
        binding_ready: bool = True,
        review_uses_semantic_signer: bool = False,
        supply_subject_image_override: str | None = None,
        semantic_attested_early: bool = False,
        invalid_dsse_signature: bool = False,
        slsa_repository_override: str | None = None,
        readiness_owner_uid_override: str | None = None,
        readiness_pod_image_override: str | None = None,
        readiness_probe_pod_override: str | None = None,
        preemption_old_node_override: str | None = None,
        preemption_replacement_same: bool = False,
        cold_nonzero_boundary: bool = False,
        reuse_cold_pod_uid: bool = False,
        quality_candidate_value: object = 1.0,
        activation_observed_at: str = "2026-08-27T20:59:30Z",
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        object,
        ServingBindings,
        Path,
        Path,
        dict[str, str],
    ]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        evidence = root / "evidence"
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        variant = catalog.model_variant("proteinmpnn-upstream-portable")
        fallback = catalog.fallback_candidate("proteinmpnn-upstream-2023-06")
        variant_value = variant.to_dict()
        source = variant_value["source"]
        binding = self.binding(catalog)
        if not binding_ready:
            binding = ServingBinding(**{**binding.__dict__, "ready": False})
        bindings = ServingBindings(
            catalog_digest=catalog.digest,
            bindings=MappingProxyType({"proteinmpnn": binding}),
        )

        expected_hashes = source["artifact"]["expected_content_sha256"]
        files = [
            {"path": f"vanilla_model_weights/v_{index:02d}.pt", "bytes": index + 1, "sha256": value}
            for index, value in enumerate(expected_hashes)
        ]
        content_digest = hashlib.sha256(canonical_bytes(files)).hexdigest()
        manifest_value = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": "proteinmpnn",
            "kind": "weights",
            "source": {
                "uri": "hf://github.com/dauparas/ProteinMPNN",
                "revision": source["revision"],
            },
            "content": {
                "digest": content_digest,
                "expanded_bytes": sum(item["bytes"] for item in files),
                "files": files,
            },
            "license": {"id": source["license"]["id"], "state": "verified"},
            "entitlement_state": "not-required",
            "owner": "fs2-serve/model-variant",
            "retention": "retained-platform",
        }
        manifest_digest = hashlib.sha256(canonical_bytes(manifest_value)).hexdigest()
        artifact_dir = evidence / "artifacts"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / f"{manifest_digest}.json").write_text(
            json.dumps(manifest_value) + "\n"
        )
        artifact_claims = {
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "source_revision": source["revision"],
            "artifact_content_digest": content_digest,
            "artifact_file_count": len(files),
            "artifact_expanded_bytes": sum(item["bytes"] for item in files),
            "artifact_file_inventory_sha256": hashlib.sha256(
                canonical_bytes(files)
            ).hexdigest(),
        }
        self.write_attestation(
            evidence,
            kind="artifacts",
            subject_schema=manifest_value["schema"],
            subject_digest=manifest_digest,
            model_id="proteinmpnn",
            claims=artifact_claims,
        )

        image_repository = "registry.example.invalid/fs2/proteinmpnn"
        image_digest = "sha256:" + digest("proteinmpnn-portable-runtime-image")
        image_reference = f"{image_repository}@{image_digest}"
        oci_subject = hashlib.sha256(
            canonical_bytes({"repository": image_repository, "digest": image_digest})
        ).hexdigest()
        build = {
            "source_repository": source["repository"],
            "source_revision": source["revision"],
            "source_tree_sha256": digest("proteinmpnn-source-tree"),
            "materials_sha256": digest("proteinmpnn-build-materials"),
            "builder_identity_sha256": digest("fs2-builder-identity"),
            "build_type": "fs2-serve.nebius.ai/open-runtime-build/v1",
        }
        build_identity = hashlib.sha256(canonical_bytes(build)).hexdigest()

        def supply_object(subject_kind: str, payload: dict[str, object], role: str) -> str:
            value = {
                "schema": "fs2-serve.nebius.ai/model-variant-supply-object/v1",
                "object_kind": subject_kind,
                "variant_id": variant.variant_id,
                "variant_digest": variant.digest,
                "observed_at": "2026-08-27T20:50:00Z",
                "payload": payload,
                "valid_until": "2026-08-27T23:00:00Z",
            }
            raw_digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
            claims = {
                "variant_digest": variant.digest,
                "object_kind": subject_kind,
                "image_digest": image_digest,
                "oci_subject_sha256": oci_subject,
                "source_revision": source["revision"],
                "build_identity_sha256": build_identity,
                "raw_object_sha256": raw_digest,
            }
            return self.write_raw_object(
                evidence, "variant-supply-objects", value, claims, role=role
            )

        dsse_statement = {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": supply_subject_image_override or image_repository, "digest": {"sha256": image_digest[7:]}}],
            "predicateType": "https://cosign.sigstore.dev/attestation/v1",
            "predicate": {
                "source_revision": source["revision"],
                "oci_subject_sha256": oci_subject,
                "build_identity_sha256": build_identity,
            },
        }
        dsse_bytes = canonical_bytes(dsse_statement)
        payload_type = "application/vnd.in-toto+json"
        pae = (
            b"DSSEv1 " + str(len(payload_type)).encode() + b" " + payload_type.encode()
            + b" " + str(len(dsse_bytes)).encode() + b" " + dsse_bytes
        )
        signature_key = self.role_keys["supply-signature"]
        slsa_statement = {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [{"name": image_repository, "digest": {"sha256": image_digest[7:]}}],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": build["build_type"],
                    "externalParameters": {
                        "source_repository": slsa_repository_override or source["repository"],
                        "source_revision": source["revision"],
                        "source_tree_sha256": build["source_tree_sha256"],
                    },
                    "resolvedDependencies": [
                        {
                            "uri": source["repository"],
                            "digest": {"gitCommit": source["revision"]},
                        },
                        {
                            "uri": "fs2://build-materials",
                            "digest": {"sha256": build["materials_sha256"]},
                        },
                    ],
                },
                "runDetails": {
                    "builder": {"id": build["builder_identity_sha256"]},
                    "metadata": {
                        "invocationId": digest("variant-build-invocation"),
                        "startedOn": "2026-08-27T20:40:00Z",
                        "finishedOn": "2026-08-27T20:49:00Z",
                    },
                },
            },
        }
        spdx = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "proteinmpnn-runtime-sbom",
            "documentNamespace": "https://fs2.invalid/spdx/" + digest("spdx-document"),
            "creationInfo": {"created": "2026-08-27T20:49:00Z", "creators": ["Tool: syft"]},
            "packages": [{
                "SPDXID": "SPDXRef-Package-runtime",
                "name": image_repository,
                "versionInfo": image_digest,
                "downloadLocation": "NOASSERTION",
                "checksums": [{"algorithm": "SHA256", "checksumValue": image_digest[7:]}],
                "externalRefs": [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:oci/{image_repository}@{image_digest}",
                }],
            }],
        }
        attestation_subjects = {
            "signature_object_digest": supply_object("signature", {
                "format": "cosign-dsse-bundle",
                "payload_type": payload_type,
                "payload": base64.b64encode(dsse_bytes).decode(),
                "signatures": [{
                    "key_id": public_key_id(signature_key.public_key()),
                    "sig": base64.urlsafe_b64encode(
                        signature_key.sign(pae + (b"-wrong" if invalid_dsse_signature else b""))
                    ).rstrip(b"=").decode(),
                }],
                "signer_key_id": public_key_id(signature_key.public_key()),
                "issuer_identity_sha256": digest("trusted-issuer"),
                "trust_policy_sha256": digest("variant-image-trust-policy"),
            }, "supply-signature"),
            "provenance_object_digest": supply_object("provenance", slsa_statement, "supply-provenance"),
            "sbom_object_digest": supply_object("sbom", spdx, "supply-sbom"),
            "scan_object_digest": supply_object("scan", {
                "schema": "fs2-serve.nebius.ai/container-scan/v1",
                "image_repository": image_repository,
                "image_digest": image_digest,
                "oci_subject_sha256": oci_subject,
                "scanner": "trivy@sha256:" + digest("trivy-image"),
                "scanner_database_sha256": digest("scanner-database"),
                "scanner_database_valid_until": "2026-08-27T23:00:00Z",
                "scanned_at": "2026-08-27T20:49:00Z",
                "critical_findings": 0,
                "high_findings": 0,
            }, "supply-scan"),
        }
        license_bytes = b"ProteinMPNN upstream license fixture bytes\n"
        license_digest = hashlib.sha256(license_bytes).hexdigest()
        self.write_raw_bytes(
            evidence,
            "variant-license-artifacts",
            license_bytes,
            "fs2-serve.nebius.ai/model-variant-license-artifact/v1",
            {
                "variant_digest": variant.digest,
                "source_revision": source["revision"],
                "license_id": license_id or source["license"]["id"],
                "source_url": source["license"]["source_url"],
                "raw_license_sha256": license_digest,
            },
            role="license",
        )
        supply_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-supply-receipt/v5",
            "status": "PASS",
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "base_model_id": variant.base_model_id,
            "exposed_model_id": variant.exposed_model_id,
            "source": {
                "kind": source["kind"],
                "repository": supply_repository or source["repository"],
                "revision": source["revision"],
                "revision_url": supply_revision_url or source["revision_url"],
            },
            "artifact": {
                "manifest_schema": manifest_value["schema"],
                "manifest_sha256": manifest_digest,
                "content_sha256": content_digest,
                "file_count": len(files) + supply_file_count_delta,
                "expanded_bytes": sum(item["bytes"] for item in files)
                + supply_bytes_delta,
                "file_inventory_sha256": hashlib.sha256(
                    canonical_bytes(files)
                ).hexdigest(),
            },
            "license": {
                "id": license_id or source["license"]["id"],
                "source_url": source["license"]["source_url"],
                "artifact_sha256": license_digest,
                "revision": source["revision"],
            },
            "runtime": {
                "architecture": "portable",
                "repository": image_repository,
                "digest": image_digest,
                "reference": image_reference,
                "oci_subject_sha256": oci_subject_override or oci_subject,
            },
            "build": build,
            "attestations": attestation_subjects,
            "valid_until": "2026-08-27T23:00:00Z",
        }
        supply_claims = {
            "variant_digest": variant.digest,
            "source_revision": source["revision"],
            "artifact_manifest_digest": manifest_digest,
            "artifact_content_digest": content_digest,
            "license_artifact_sha256": supply_unsigned["license"]["artifact_sha256"],
            "image_reference": image_reference,
            "image_digest": image_digest,
            "oci_subject_sha256": supply_unsigned["runtime"]["oci_subject_sha256"],
            "build_identity_sha256": build_identity,
            "supply_attestation_set_sha256": hashlib.sha256(
                canonical_bytes(attestation_subjects)
            ).hexdigest(),
        }
        supply_digest, supply = self.write_receipt(
            evidence, "variant-supplies", supply_unsigned, supply_claims
        )

        worker = {
            "project_sha256": digest("rene-us-north-project"),
            "region": "us-north1",
            "cluster_sha256": digest("fs2-serve-cluster"),
            "node_name": "fs2-b300-burst-unit",
            "node_uid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "worker_image_reference": "registry.example.invalid/fs2/worker@sha256:"
            + digest("worker-image"),
            "worker_image_digest": "sha256:" + digest("worker-image"),
            "driver_version": "580.95.05",
            "cuda_version": "13.0.1",
            "device_plugin_reference": "registry.example.invalid/fs2/device-plugin@sha256:"
            + digest("device-plugin"),
            "device_plugin_digest": "sha256:" + digest("device-plugin"),
            "gpu_class": "NVIDIA B300",
            "gpu_architecture": runtime_gpu_architecture,
            "compute_capability": "10.3",
            "allocated_gpu_count": 1,
            "gpu_uuids": ["GPU-unit-b300-0001"],
        }
        runtime_subject = {
            "architecture": "portable",
            "image_repository": image_repository,
            "image_reference": image_reference,
            "image_digest": image_digest,
            "source_revision": runtime_revision or source["revision"],
            "argv_sha256": digest("proteinmpnn-mounted-argv"),
            "execution_identity_sha256": digest("proteinmpnn-portable-execution"),
            "network_startup": "deny-egress-mounted-content-address-only",
        }
        kernels = [
            {
                "name": "proteinmpnn_attention_forward_sm103",
                "architecture": kernel_architecture,
                "binary_sha256": digest("proteinmpnn-attention-cubin"),
                "dispatch_count": 42,
            }
        ]
        runtime_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-runtime-tuple/v1",
            "status": "PASS",
            "captured_at": "2026-08-27T20:59:00Z",
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply_digest,
            "worker": worker,
            "runtime": runtime_subject,
            "artifact": {
                "manifest_sha256": manifest_digest,
                "content_sha256": content_digest,
                "mount_read_only": True,
                "network_denied_startup": True,
            },
            "kernels": kernels,
            "valid_until": "2026-08-27T23:00:00Z",
        }
        runtime_claims = {
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply_digest,
            "worker_identity_sha256": hashlib.sha256(canonical_bytes(worker)).hexdigest(),
            "runtime_identity_sha256": hashlib.sha256(
                canonical_bytes(runtime_subject)
            ).hexdigest(),
            "artifact_manifest_digest": manifest_digest,
            "artifact_content_digest": content_digest,
            "kernel_dispatch_set_sha256": hashlib.sha256(
                canonical_bytes(kernels)
            ).hexdigest(),
        }
        runtime_digest, _ = self.write_receipt(
            evidence, "variant-runtime-tuples", runtime_unsigned, runtime_claims
        )

        semantic_contract = catalog.semantic_request_contract("proteinmpnn")
        validator = catalog.model("proteinmpnn").to_dict()["semantic_validator"]
        gateway = self.gateway_subject(binding)
        semantic_digests: dict[str, str] = {}

        def semantic(attempt_id: str, observed_at: str) -> str:
            requests = [
                {
                    "request_id": request_id,
                    "request_sha256": request_sha256,
                    "response_sha256": digest(f"{attempt_id}:response:{index}"),
                    "validator_source_sha256": validator["source_sha256"],
                    "validator_fixture_sha256": validator["fixture_sha256"],
                    "validation_result_sha256": digest(
                        f"{attempt_id}:validation-result:{index}"
                    ),
                    "semantic_valid": True,
                }
                for index, (request_id, request_sha256) in enumerate(
                    zip(
                        semantic_contract.request_ids,
                        semantic_contract.request_sha256,
                        strict=True,
                    )
                )
            ]
            unsigned = {
                "schema": "fs2-serve.nebius.ai/model-variant-semantic-receipt/v2",
                "status": "PASS",
                "variant_id": variant.variant_id,
                "variant_digest": variant.digest,
                "attempt_id": attempt_id,
                "observed_at": observed_at,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "semantic_contract_digest": semantic_contract.digest,
                "gateway": gateway,
                "operation": semantic_contract.invocation["operation"],
                "protocol": semantic_contract.invocation["protocol"],
                "requests": requests,
                "valid_until": "2026-08-27T23:00:00Z",
            }
            claims = {
                "variant_digest": variant.digest,
                "attempt_id": attempt_id,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "semantic_contract_digest": semantic_contract.digest,
                "gateway_identity_sha256": hashlib.sha256(
                    canonical_bytes(gateway)
                ).hexdigest(),
                "request_result_set_sha256": hashlib.sha256(
                    canonical_bytes(requests)
                ).hexdigest(),
            }
            result, _ = self.write_receipt(
                evidence,
                "variant-semantics",
                unsigned,
                claims,
                issued_at=(
                    "2026-08-27T20:00:00Z"
                    if semantic_attested_early
                    else "2026-08-27T22:20:00Z"
                ),
            )
            semantic_digests[attempt_id] = result
            return result

        cold_base = datetime(2026, 8, 27, 21, 1, 0, tzinfo=timezone.utc)

        def stamp(value: datetime) -> str:
            return value.isoformat().replace("+00:00", "Z")

        def k8s_observation(
            object_kind: str,
            observed_at: str,
            obj: dict[str, object],
            *,
            role: str,
        ) -> str:
            value = {
                "schema": "fs2-serve.nebius.ai/model-variant-kubernetes-observation/v1",
                "object_kind": object_kind,
                "observed_at": observed_at,
                "valid_until": "2026-08-27T23:00:00Z",
                "observer": {
                    "source": "kubernetes-apiserver",
                    "cluster_identity_sha256": worker["cluster_sha256"],
                    "api_server_identity_sha256": digest("kubernetes-api-server"),
                    "service_account_uid": "abababab-abab-abab-abab-abababababab",
                    "complete": True,
                },
                "object": obj,
            }
            raw_digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
            return self.write_raw_object(
                evidence,
                "variant-kubernetes-observations",
                value,
                {
                    "object_kind": object_kind,
                    "cluster_identity_sha256": worker["cluster_sha256"],
                    "raw_object_sha256": raw_digest,
                },
                role=role,
            )

        def cold_boundary(
            attempt_id: str,
            pod_uid: str,
            started: datetime,
            completed: datetime,
        ) -> str:
            absence_at = started - timedelta(seconds=5)
            process_at = started - timedelta(seconds=3)
            ready_at = started - timedelta(seconds=1)
            process_identity = digest(f"{attempt_id}:process")
            cache_generation = digest(f"{attempt_id}:cache-generation")
            refs = {
                "pod_absence": k8s_observation(
                    "pod_absence",
                    stamp(absence_at),
                    {
                        "namespace": "fs2-models",
                        "label_selector": "fs2.nebius/model-id=proteinmpnn",
                        "continue": "",
                        "remainingItemCount": 0,
                        "items": [],
                        "gpu_processes": 1 if cold_nonzero_boundary else 0,
                        "replicas": 1 if cold_nonzero_boundary else 0,
                    },
                    role="cold-boundary",
                ),
                "pod": k8s_observation(
                    "pod",
                    stamp(process_at),
                    {
                        "namespace": "fs2-models",
                        "name": f"proteinmpnn-{attempt_id}",
                        "uid": pod_uid,
                        "resourceVersion": f"51{attempt_id[-2:]}",
                        "nodeName": worker["node_name"],
                        "imageID": image_reference,
                        "startedAt": stamp(process_at),
                    },
                    role="cold-boundary",
                ),
                "node": k8s_observation(
                    "node",
                    stamp(process_at),
                    {"name": worker["node_name"], "uid": worker["node_uid"], "resourceVersion": "52001"},
                    role="cold-boundary",
                ),
                "pod_resources": k8s_observation(
                    "pod_resources",
                    stamp(ready_at),
                    {"pod_uid": pod_uid, "container": "model", "resource_name": "nvidia.com/gpu", "device_ids": [worker["gpu_uuids"][0]]},
                    role="cold-boundary",
                ),
                "process_cache": k8s_observation(
                    "process_cache",
                    stamp(ready_at),
                    {"pod_uid": pod_uid, "process_started_at": stamp(process_at), "process_identity_sha256": process_identity, "gpu_clients_before": 0, "cache_state": "cold-empty-or-version-absent", "cache_generation_sha256": cache_generation, "writer_count": 0},
                    role="cold-boundary",
                ),
            }
            unsigned = {
                "schema": "fs2-serve.nebius.ai/model-variant-cold-boundary-receipt/v1",
                "status": "PASS",
                "variant_id": variant.variant_id,
                "variant_digest": variant.digest,
                "attempt_id": attempt_id,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "pod_uid": pod_uid,
                "node_uid": worker["node_uid"],
                "gpu_uuid": worker["gpu_uuids"][0],
                "process_identity_sha256": process_identity,
                "cache_generation_sha256": cache_generation,
                "observations": refs,
                "ready_at": stamp(ready_at),
                "t0": stamp(started),
                "completed_at": stamp(completed),
                "valid_until": "2026-08-27T23:00:00Z",
            }
            claims = {
                "variant_digest": variant.digest,
                "attempt_id": attempt_id,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "observation_set_sha256": hashlib.sha256(canonical_bytes(refs)).hexdigest(),
                "pod_uid": pod_uid,
                "node_uid": worker["node_uid"],
                "gpu_uuid": worker["gpu_uuids"][0],
                "process_identity_sha256": process_identity,
                "cache_generation_sha256": cache_generation,
            }
            return self.write_receipt(
                evidence, "variant-cold-boundaries", unsigned, claims
            )[0]

        def cohort(kind: str, count: int) -> tuple[str, dict[str, object]]:
            attempts = []
            cohort_offset = 0 if kind == "cold" else cold_attempts * 6 + 10
            for index in range(count):
                attempt_id = (
                    "cold-01"
                    if duplicate_cross_cohort_attempt_id and kind == "warm" and index == 0
                    else f"{kind}-{index + 1:02d}"
                )
                started = (
                    cold_base
                    if overlap_attempts
                    else cold_base + timedelta(seconds=cohort_offset + index * 6)
                )
                completed = started + timedelta(seconds=5)
                pod_uid = (
                    (
                        "10000001-1111-1111-1111-000000000001"
                        if reuse_cold_pod_uid
                        else f"1000000{index + 1}-1111-1111-1111-{index + 1:012d}"
                    )
                    if kind == "cold"
                    else "dededede-dede-dede-dede-dededededede"
                )
                node_uid = worker["node_uid"]
                gpu_uuid = worker["gpu_uuids"][0]
                if kind == "warm" and index == 0:
                    pod_uid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
                    node_uid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
                    gpu_uuid = "GPU-unit-b300-0002"
                semantic_digest = (
                    semantic_digests[f"{kind}-01"]
                    if replay_semantic and index == count - 1 and index > 0
                    else semantic(attempt_id, stamp(completed))
                )
                attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "status": "PASS",
                        "t0": stamp(started),
                        "completed_at": stamp(completed),
                        "duration_seconds": 5.0,
                        "failure_reason": None,
                        "semantic_receipt_digest": semantic_digest,
                        "output_sha256": digest("proteinmpnn-deterministic-output-set"),
                        "kernel_dispatch_sha256": hashlib.sha256(
                            canonical_bytes(kernels)
                        ).hexdigest(),
                        "pre_t0_work_sha256": digest(f"{attempt_id}:pre-t0"),
                        "pod_uid": pod_uid,
                        "node_uid": node_uid,
                        "gpu_uuid": gpu_uuid,
                        "cold_boundary_receipt_digest": (
                            cold_boundary(attempt_id, pod_uid, started, completed)
                            if kind == "cold" else None
                        ),
                    }
                )
            unsigned = {
                "schema": "fs2-serve.nebius.ai/model-variant-cohort/v3",
                "status": "PASS",
                "variant_id": variant.variant_id,
                "variant_digest": variant.digest,
                "cohort_kind": kind,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "attempts_total": len(attempts),
                "successes_total": len(attempts),
                "failures_total": 0,
                "failures_in_denominator": True,
                "attempts": attempts,
                "valid_until": "2026-08-27T23:00:00Z",
            }
            claims = {
                "variant_digest": variant.digest,
                "cohort_kind": kind,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "attempt_set_sha256": hashlib.sha256(
                    canonical_bytes(attempts)
                ).hexdigest(),
                "attempts_total": len(attempts),
                "successes_total": len(attempts),
                "failures_total": 0,
            }
            return self.write_receipt(evidence, "variant-cohorts", unsigned, claims)

        cold_digest, cold = cohort("cold", cold_attempts)
        warm_digest, warm = cohort("warm", warm_attempts)
        warm_ids = [item["attempt_id"] for item in warm["attempts"]]
        ready_pod_uid = "dededede-dede-dede-dede-dededededede"
        pod_ip = "10.20.30.40"
        service_selector = {
            "app.kubernetes.io/name": "proteinmpnn",
            "fs2.nebius/model-id": "proteinmpnn",
        }
        readiness_objects = {
            "service": k8s_observation("service", "2026-08-27T21:00:00Z", {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"namespace": binding.backend_namespace, "name": binding.backend_service_name, "uid": gateway["backend_service_uid"], "resourceVersion": "41001"},
                "spec": {"selector": service_selector, "ports": [{"name": "http", "port": binding.backend_port, "targetPort": "http"}]},
            }, role="backend-readiness"),
            "endpoint_slice": k8s_observation("endpoint_slice", "2026-08-27T21:00:01Z", {
                "apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
                "metadata": {"namespace": binding.backend_namespace, "name": "proteinmpnn-unit", "uid": "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd", "resourceVersion": "41002", "labels": {"kubernetes.io/service-name": binding.backend_service_name}, "ownerReferences": [{"apiVersion": "v1", "kind": "Service", "name": binding.backend_service_name, "uid": readiness_owner_uid_override or gateway["backend_service_uid"], "controller": True}]},
                "addressType": "IPv4", "ports": [{"name": "http", "port": binding.backend_port, "protocol": "TCP"}],
                "endpoints": [{"addresses": [pod_ip], "conditions": {"ready": True, "serving": True, "terminating": False}, "targetRef": {"kind": "Pod", "namespace": binding.backend_namespace, "name": "proteinmpnn-ready", "uid": ready_pod_uid}, "nodeName": worker["node_name"]}],
            }, role="backend-readiness"),
            "pod": k8s_observation("pod", "2026-08-27T21:00:01Z", {
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"namespace": binding.backend_namespace, "name": "proteinmpnn-ready", "uid": ready_pod_uid, "resourceVersion": "41003", "labels": service_selector},
                "spec": {"nodeName": worker["node_name"], "containers": [{"name": "model", "image": readiness_pod_image_override or image_reference, "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"nvidia.com/gpu": 1}}, "ports": [{"name": "http", "containerPort": binding.backend_port}]}]},
                "status": {"phase": "Running", "podIP": pod_ip, "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": [{"name": "model", "ready": True, "image": readiness_pod_image_override or image_reference, "imageID": readiness_pod_image_override or image_reference}]},
            }, role="backend-readiness"),
            "node": k8s_observation("node", "2026-08-27T21:00:01Z", {
                "apiVersion": "v1", "kind": "Node",
                "metadata": {"name": worker["node_name"], "uid": worker["node_uid"], "resourceVersion": "41004", "labels": {"nvidia.com/gpu.product": "NVIDIA-B300", "nvidia.com/gpu.compute.major": "10", "nvidia.com/gpu.compute.minor": "3"}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }, role="backend-readiness"),
            "pod_resources": k8s_observation("pod_resources", "2026-08-27T21:00:01Z", {
                "podUid": ready_pod_uid, "namespace": binding.backend_namespace, "name": "proteinmpnn-ready",
                "containers": [{"name": "model", "devices": [{"resourceName": "nvidia.com/gpu", "deviceIds": [worker["gpu_uuids"][0]]}]}],
            }, role="backend-readiness"),
            "probe": k8s_observation("probe", "2026-08-27T21:00:02Z", {
                "transport": "gateway-proxy", "gateway_service_uid": binding.gateway_service_uid, "backend_service_uid": gateway["backend_service_uid"], "pod_uid": readiness_probe_pod_override or ready_pod_uid, "pod_ip": pod_ip, "method": "GET", "path": "/health/ready", "port": binding.backend_port, "status": 200,
            }, role="backend-readiness"),
        }
        readiness_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-backend-readiness-receipt/v2",
            "status": "PASS",
            "observed_at": "2026-08-27T21:00:02Z",
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "serving_binding_digest": binding.binding_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest_digest,
            "observation_digests": readiness_objects,
            "valid_until": "2026-08-27T23:00:00Z",
        }
        readiness_claims = {
            "variant_digest": variant.digest,
            "serving_binding_digest": binding.binding_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest_digest,
            "observation_set_sha256": hashlib.sha256(canonical_bytes(readiness_objects)).hexdigest(),
            "service_uid": gateway["backend_service_uid"],
            "pod_uid": ready_pod_uid,
            "node_uid": worker["node_uid"],
            "gpu_uuid": worker["gpu_uuids"][0],
            "probe_sha256": hashlib.sha256(canonical_bytes({"transport": "gateway-proxy", "gateway_service_uid": binding.gateway_service_uid, "backend_service_uid": gateway["backend_service_uid"], "pod_uid": ready_pod_uid, "pod_ip": pod_ip, "method": "GET", "path": "/health/ready", "port": binding.backend_port, "status": 200})).hexdigest(),
        }
        readiness_digest, _ = self.write_receipt(
            evidence,
            "variant-backend-readiness",
            readiness_unsigned,
            readiness_claims,
        )

        replacement = {
            "pod_uid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "node_uid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "gpu_uuid": "GPU-unit-b300-0002",
        }
        if preemption_replacement_same:
            replacement = {
                "pod_uid": ready_pod_uid,
                "node_uid": worker["node_uid"],
                "gpu_uuid": worker["gpu_uuids"][0],
            }
        old_identity = {
            "pod_uid": ready_pod_uid,
            "node_uid": preemption_old_node_override or worker["node_uid"],
            "gpu_uuid": worker["gpu_uuids"][0],
        }
        preemption_objects = {
            "old_pod": k8s_observation("old_pod", "2026-08-27T21:01:18Z", {"uid": ready_pod_uid, "node_uid": worker["node_uid"], "imageID": image_reference}, role="preemption"),
            "old_node": k8s_observation("old_node", "2026-08-27T21:01:18Z", {"uid": worker["node_uid"]}, role="preemption"),
            "old_pod_resources": k8s_observation("old_pod_resources", "2026-08-27T21:01:18Z", {"pod_uid": ready_pod_uid, "gpu_uuid": worker["gpu_uuids"][0]}, role="preemption"),
            "event": k8s_observation("event", "2026-08-27T21:01:19Z", {"reason": "Preempted", "regarding_uid": ready_pod_uid, "event_time": "2026-08-27T21:01:19Z", "attempt_id": warm_ids[0]}, role="preemption"),
            "old_fence": k8s_observation("old_fence", "2026-08-27T21:01:20Z", {"pod_uid": ready_pod_uid, "pod_absent": True, "node_fenced": True, "gpu_clients": 0, "fencing_token": 19}, role="preemption"),
            "replacement_pod": k8s_observation("replacement_pod", "2026-08-27T21:01:25Z", {"uid": replacement["pod_uid"], "node_uid": replacement["node_uid"], "imageID": image_reference, "ready": True}, role="preemption"),
            "replacement_node": k8s_observation("replacement_node", "2026-08-27T21:01:25Z", {"uid": replacement["node_uid"], "gpu_class": "NVIDIA B300", "compute_capability": "10.3"}, role="preemption"),
            "replacement_pod_resources": k8s_observation("replacement_pod_resources", "2026-08-27T21:01:25Z", {"pod_uid": replacement["pod_uid"], "gpu_uuid": replacement["gpu_uuid"]}, role="preemption"),
        }
        preemption_semantic = warm["attempts"][0]["semantic_receipt_digest"]
        if preemption_cross_pair:
            preemption_semantic = warm["attempts"][1]["semantic_receipt_digest"]
        preemption_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-preemption-receipt/v1",
            "status": "PASS", "variant_id": variant.variant_id, "variant_digest": variant.digest,
            "attempt_id": warm_ids[0], "semantic_receipt_digest": preemption_semantic,
            "runtime_tuple_digest": runtime_digest, "artifact_manifest_digest": manifest_digest,
            "backend_readiness_receipt_digest": readiness_digest,
            "observations": preemption_objects, "old_identity": old_identity,
            "replacement_identity": replacement, "old_fencing_token": 19,
            "replacement_fencing_token": 20, "observed_at": "2026-08-27T21:01:27Z",
            "valid_until": "2026-08-27T23:00:00Z",
        }
        preemption_claims = {
            "variant_digest": variant.digest, "attempt_id": warm_ids[0],
            "semantic_receipt_digest": preemption_semantic,
            "runtime_tuple_digest": runtime_digest, "artifact_manifest_digest": manifest_digest,
            "backend_readiness_receipt_digest": readiness_digest,
            "observation_set_sha256": hashlib.sha256(canonical_bytes(preemption_objects)).hexdigest(),
            "old_identity_sha256": hashlib.sha256(canonical_bytes(old_identity)).hexdigest(),
            "replacement_identity_sha256": hashlib.sha256(canonical_bytes(replacement)).hexdigest(),
            "old_fencing_token": 19, "replacement_fencing_token": 20,
        }
        preemption_digest, preemption = self.write_receipt(
            evidence, "variant-preemptions", preemption_unsigned, preemption_claims
        )

        def lifecycle(
            action: str,
            observed_at: str,
            operation_id: str,
            previous_fencing_token: int,
            fencing_token: int,
            replicas: dict[str, int],
        ) -> tuple[str, dict[str, object]]:
            unsigned = {
                "schema": "fs2-serve.nebius.ai/model-variant-lifecycle-receipt/v1",
                "status": "PASS",
                "action": action,
                "observed_at": observed_at,
                "variant_id": variant.variant_id,
                "variant_digest": variant.digest,
                "serving_binding_digest": binding.binding_digest,
                "scale_contract_digest": catalog.scale_contract("proteinmpnn").digest,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "backend_readiness_receipt_digest": readiness_digest,
                "operation_id": operation_id,
                "previous_fencing_token": previous_fencing_token,
                "fencing_token": fencing_token,
                "replicas": replicas,
                "backend_service_uid": gateway["backend_service_uid"],
                "node_uid": worker["node_uid"],
                "gpu_uuid": worker["gpu_uuids"][0],
                "artifact_retained": True,
                "valid_until": "2026-08-27T23:00:00Z",
            }
            claims = {
                "variant_digest": variant.digest,
                "action": action,
                "serving_binding_digest": binding.binding_digest,
                "scale_contract_digest": catalog.scale_contract("proteinmpnn").digest,
                "runtime_tuple_digest": runtime_digest,
                "artifact_manifest_digest": manifest_digest,
                "backend_readiness_receipt_digest": readiness_digest,
                "operation_id": operation_id,
                "previous_fencing_token": previous_fencing_token,
                "fencing_token": fencing_token,
                "replica_transition_sha256": hashlib.sha256(
                    canonical_bytes(replicas)
                ).hexdigest(),
                "backend_service_uid": gateway["backend_service_uid"],
                "node_uid": worker["node_uid"],
                "gpu_uuid": worker["gpu_uuids"][0],
            }
            return self.write_receipt(evidence, "variant-lifecycles", unsigned, claims)

        zero_digest, zero_lifecycle = lifecycle(
            "activate",
            activation_observed_at,
            "variant-activate-unit",
            18,
            19,
            {"previous": 0, "desired": 1, "observed": 1},
        )
        return_digest, return_lifecycle = lifecycle(
            "deactivate",
            "2026-08-27T22:19:00Z",
            "variant-deactivate-unit",
            19,
            20,
            {"previous": 1, "desired": 0, "observed": 0},
        )
        measurement = {
            "compute_capability": "10.3",
            "gpu_architecture": "sm_103",
            "warm_attempts_total": len(warm["attempts"]),
            "warm_failures_total": (
                0 if warm_failures_override is None else warm_failures_override
            ),
            "warm_failure_rate": 0.0,
            "max_warm_failure_rate": 0.1,
            "cold_attempts_total": len(cold["attempts"]),
            "cold_failures_total": (
                0 if cold_failures_override is None else cold_failures_override
            ),
            "cold_failure_rate": 0.0,
            "max_cold_failure_rate": 0.1,
            "failures_in_denominator": True,
            "determinism_attempt_ids": warm_ids[:3],
            "kernel_dispatch_attempt_ids": warm_ids[:3],
            "semantic_responses_per_success": 2,
        }
        baseline = variant_value["relationship"]["vendor_baseline"]
        quality = {
            "status": "PASS",
            "comparator_model_id": baseline["model_id"],
            "comparator_execution_identity_sha256": baseline[
                "execution_identity_sha256"
            ],
            "dataset_sha256": digest("proteinmpnn-quality-corpus"),
            "metric": "success-rate",
            "candidate_value": quality_candidate_value,
            "baseline_value": 1.0,
            "allowed_regression": 0.0,
        }
        lifecycle = {
            "status": "PASS",
            "zero_to_ready_operation_id": "variant-activate-unit",
            "return_to_zero_operation_id": "variant-deactivate-unit",
            "zero_to_ready_receipt_sha256": zero_digest,
            "return_to_zero_receipt_sha256": return_digest,
            "initial_replicas": 0,
            "ready_replicas": 1,
            "final_replicas": 0,
            "activation_fencing_token": 19,
            "deactivation_fencing_token": 20,
            "artifact_retained": True,
        }
        qualification_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-qualification-receipt/v5",
            "status": "PASS",
            "variant_id": variant.variant_id,
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest_digest,
            "semantic_contract_digest": semantic_contract.digest,
            "cold_cohort_digest": cold_digest,
            "warm_cohort_digest": warm_digest,
            "backend_readiness_receipt_digest": readiness_digest,
            "measurement": measurement,
            "quality": quality,
            "preemption_receipt_digest": preemption_digest,
            "lifecycle": lifecycle,
            "gateway": gateway,
            "vendor_baseline": baseline,
            "valid_until": "2026-08-27T23:00:00Z",
        }
        qualification_claims = {
            "variant_digest": variant.digest,
            "supply_receipt_digest": supply_digest,
            "runtime_tuple_digest": runtime_digest,
            "artifact_manifest_digest": manifest_digest,
            "cold_cohort_digest": cold_digest,
            "warm_cohort_digest": warm_digest,
            "backend_readiness_receipt_digest": readiness_digest,
            "measurement_sha256": hashlib.sha256(
                canonical_bytes(measurement)
            ).hexdigest(),
            "quality_sha256": hashlib.sha256(canonical_bytes(quality)).hexdigest(),
            "preemption_receipt_digest": preemption_digest,
            "lifecycle_sha256": hashlib.sha256(
                canonical_bytes(lifecycle)
            ).hexdigest(),
            "gateway_identity_sha256": hashlib.sha256(
                canonical_bytes(gateway)
            ).hexdigest(),
            "vendor_baseline_sha256": hashlib.sha256(
                canonical_bytes(baseline)
            ).hexdigest(),
        }
        qualification_digest, _ = self.write_receipt(
            evidence,
            "variant-qualifications",
            qualification_unsigned,
            qualification_claims,
        )
        review_evidence = {
            "artifact_manifest_digest": manifest_digest,
            "supply_receipt_digest": supply_digest,
            "runtime_tuple_digest": runtime_digest,
            "cold_cohort_digest": cold_digest,
            "warm_cohort_digest": warm_digest,
            "qualification_receipt_digest": qualification_digest,
            "backend_readiness_receipt_digest": readiness_digest,
            "preemption_receipt_digest": preemption_digest,
            "zero_to_ready_receipt_digest": zero_digest,
            "return_to_zero_receipt_digest": return_digest,
        }
        policy = self.trust_policy()
        policy_digest = hashlib.sha256(canonical_bytes(policy)).hexdigest()
        review_key = (
            self.private_key
            if review_uses_semantic_signer or not independent_review
            else self.review_private_key
        )
        review_unsigned = {
            "schema": "fs2-serve.nebius.ai/model-variant-review-receipt/v4",
            "status": "PASS",
            "decision": "approve-route",
            "variant_id": variant.variant_id,
            "candidate_id": fallback.candidate_id,
            "candidate_digest": fallback.digest,
            "runtime_profile": "portable",
            "variant_digest": variant.digest,
            "canonical_model_digest": catalog.model("proteinmpnn").digest,
            "serving_binding_digest": binding.binding_digest,
            "scale_contract_digest": catalog.scale_contract("proteinmpnn").digest,
            "attestor_policy_sha256": policy_digest,
            **review_evidence,
            "reviewer_identity_sha256": hashlib.sha256(
                public_key_id(review_key.public_key()).encode()
            ).hexdigest(),
            "review_commit": digest("independent-variant-review")[:40],
            "valid_until": "2026-08-27T23:00:00Z",
        }
        review_claims = {
            "variant_id": variant.variant_id,
            "candidate_id": fallback.candidate_id,
            "candidate_digest": fallback.digest,
            "runtime_profile": "portable",
            "variant_digest": variant.digest,
            "canonical_model_digest": catalog.model("proteinmpnn").digest,
            "serving_binding_digest": binding.binding_digest,
            "scale_contract_digest": catalog.scale_contract("proteinmpnn").digest,
            "attestor_policy_sha256": policy_digest,
            **review_evidence,
            "decision": "approve-route",
            "reviewer_identity_sha256": review_unsigned["reviewer_identity_sha256"],
            "review_commit": review_unsigned["review_commit"],
        }
        review_digest, _ = self.write_receipt(
            evidence,
            "variant-reviews",
            review_unsigned,
            review_claims,
            private_key=review_key,
            issued_at="2026-08-27T22:21:00Z",
        )
        overlay = {
            "schema": "fs2-serve.nebius.ai/model-variant-promotions/v4",
            "route_authority": "signed-live-evidence-only",
            "catalog_digest": catalog.digest,
            "attestor_policy_sha256": policy_digest,
            "promotions": {
                variant.variant_id: {
                    "variant_id": variant.variant_id,
                    "candidate_id": "proteinmpnn-upstream-2023-06",
                    "candidate_digest": fallback.digest,
                    "runtime_profile": "portable",
                    "variant_digest": variant.digest,
                    "base_model_id": variant.base_model_id,
                    "exposed_model_id": variant.exposed_model_id,
                    "canonical_model_digest": catalog.model("proteinmpnn").digest,
                    "serving_binding_digest": binding.binding_digest,
                    "scale_contract_digest": catalog.scale_contract("proteinmpnn").digest,
                    "enabled": True,
                    "valid_until": "2026-08-27T23:00:00Z",
                    "backend_service_uid": gateway["backend_service_uid"],
                    "evidence_session_id": self.session_id,
                    "artifact_manifest_digest": manifest_digest,
                    "supply_receipt_digest": supply_digest,
                    "runtime_tuple_digest": runtime_digest,
                    "cold_cohort_digest": cold_digest,
                    "warm_cohort_digest": warm_digest,
                    "qualification_receipt_digest": qualification_digest,
                    "backend_readiness_receipt_digest": readiness_digest,
                    "preemption_receipt_digest": preemption_digest,
                    "zero_to_ready_receipt_digest": zero_digest,
                    "return_to_zero_receipt_digest": return_digest,
                    "independent_review_receipt_digest": review_digest,
                }
            },
        }
        overlay_path = root / "variant-promotions.json"
        overlay_path.write_text(json.dumps(overlay) + "\n")
        evidence.chmod(0o700)
        for path in evidence.rglob("*"):
            path.chmod(0o750 if path.is_dir() else 0o640)
        return temporary, catalog, bindings, overlay_path, evidence, self.trusted()

    def load(self, fixture):
        _, catalog, bindings, overlay, evidence, trust = fixture
        return load_variant_gateway_catalog(
            catalog,
            bindings,
            overlay,
            evidence_root=evidence,
            trusted_attestors=trust,
            trusted_attestor_policy=self.trust_policy(),
            validation_time=self.validation_time,
        )

    def test_signed_live_overlay_is_the_only_positive_variant_route(self) -> None:
        fixture = self.build_fixture()
        catalog = self.load(fixture)
        self.assertEqual(
            ("proteinmpnn-upstream-portable",),
            catalog.routable_variant_ids(self.validation_time),
        )
        public = catalog.public_models(self.validation_time)[0]
        self.assertTrue(public["routable"])
        self.assertEqual("proteinmpnn-upstream-2023-06", public["candidate_id"])
        self.assertEqual("portable", public["runtime_profile"])
        self.assertEqual("proteinmpnn", public["model_id"])
        self.assertEqual("NVIDIA B300", public["backend"]["gpu_class"])
        self.assertNotIn("service_origin", json.dumps(public))
        self.assertNotIn("backend_service_uid", json.dumps(public))
        self.assertNotIn("activation", json.dumps(public))
        self.assertFalse(fixture[1].model_variant("proteinmpnn-upstream-portable").to_dict()["promotion"]["route_exposed"])
        self.assertEqual((), fixture[1].routable_variant_ids())

        schemas = {
            "artifacts": "artifact-manifest.schema.json",
            "variant-supplies": "model-variant-supply-receipt.schema.json",
            "variant-runtime-tuples": "model-variant-runtime-tuple.schema.json",
            "variant-semantics": "model-variant-semantic-receipt.schema.json",
            "variant-cohorts": "model-variant-cohort.schema.json",
            "variant-supply-objects": "model-variant-supply-object.schema.json",
            "variant-kubernetes-observations": "model-variant-kubernetes-observation.schema.json",
            "variant-cold-boundaries": "model-variant-cold-boundary-receipt.schema.json",
            "variant-preemptions": "model-variant-preemption-receipt.schema.json",
            "variant-backend-readiness": "model-variant-backend-readiness-receipt.schema.json",
            "variant-lifecycles": "model-variant-lifecycle-receipt.schema.json",
            "variant-qualifications": "model-variant-qualification-receipt.schema.json",
            "variant-reviews": "model-variant-review-receipt.schema.json",
        }
        overlay_schema = json.loads(
            (CATALOG_ROOT / "schema" / "model-variant-promotions.schema.json").read_text()
        )
        Draft202012Validator(overlay_schema).validate(json.loads(fixture[3].read_text()))
        for kind, schema_name in schemas.items():
            schema = json.loads((CATALOG_ROOT / "schema" / schema_name).read_text())
            validator = Draft202012Validator(schema)
            for path in (fixture[4] / kind).glob("*.json"):
                validator.validate(json.loads(path.read_text()))
        signed_schema = json.loads(
            (CATALOG_ROOT / "schema" / "signed-attestation.schema.json").read_text()
        )
        validator = Draft202012Validator(signed_schema)
        for path in (fixture[4] / "attestations").rglob("*.json"):
            validator.validate(json.loads(path.read_text()))

    def test_gateway_projection_honors_supplied_time_and_still_expires(self) -> None:
        catalog = self.load(self.build_fixture())
        valid_until = datetime(2026, 8, 27, 23, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            ("proteinmpnn-upstream-portable",),
            catalog.routable_variant_ids(self.validation_time),
        )
        self.assertEqual(1, len(catalog.public_models(self.validation_time)))
        self.assertEqual((), catalog.routable_variant_ids(valid_until))
        self.assertEqual((), catalog.public_models(valid_until))

    def test_static_disabled_overlay_and_binding_bypass_fail_closed(self) -> None:
        fixture = self.build_fixture()
        _, catalog, bindings, overlay_path, evidence, trust = fixture
        value = json.loads(overlay_path.read_text())
        item = value["promotions"]["proteinmpnn-upstream-portable"]
        item.update(
            {
                "enabled": False,
                "valid_until": None,
                "backend_service_uid": None,
                "evidence_session_id": None,
                "artifact_manifest_digest": None,
                "supply_receipt_digest": None,
                "runtime_tuple_digest": None,
                "cold_cohort_digest": None,
                "warm_cohort_digest": None,
                "qualification_receipt_digest": None,
                "backend_readiness_receipt_digest": None,
                "preemption_receipt_digest": None,
                "zero_to_ready_receipt_digest": None,
                "return_to_zero_receipt_digest": None,
                "independent_review_receipt_digest": None,
            }
        )
        overlay_path.write_text(json.dumps(value) + "\n")
        promotions = load_model_variant_promotions(overlay_path, catalog, bindings)
        self.assertEqual((), promotions.routable_variant_ids())
        self.assertEqual((), bind_variant_gateway_catalog(catalog, bindings, promotions).routable_variant_ids())

        fixture = self.build_fixture()
        value = json.loads(fixture[3].read_text())
        value["promotions"]["proteinmpnn-upstream-portable"]["serving_binding_digest"] = digest(
            "foreign-serving-binding"
        )
        fixture[3].write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "bypasses its base record/serving binding"):
            self.load(fixture)

    def test_artifact_license_oci_runtime_and_cohort_substitution_fail_closed(self) -> None:
        fixture = self.build_fixture(oci_subject_override=digest("wrong-oci-subject"))
        with self.assertRaisesRegex(CatalogError, "OCI subject"):
            self.load(fixture)

        fixture = self.build_fixture(license_id="Apache-2.0")
        with self.assertRaisesRegex(CatalogError, "license"):
            self.load(fixture)

        fixture = self.build_fixture(runtime_gpu_architecture="sm_100")
        with self.assertRaisesRegex(CatalogError, "B300 SM103"):
            self.load(fixture)

        fixture = self.build_fixture(kernel_architecture="sm_100")
        with self.assertRaisesRegex(CatalogError, "kernel"):
            self.load(fixture)

        fixture = self.build_fixture(runtime_revision=digest("wrong-runtime-revision")[:40])
        with self.assertRaisesRegex(CatalogError, "execution differs from signed supply"):
            self.load(fixture)

        fixture = self.build_fixture(warm_attempts=9)
        with self.assertRaisesRegex(CatalogError, "minimum attempt"):
            self.load(fixture)

        fixture = self.build_fixture(cold_attempts=1)
        with self.assertRaisesRegex(CatalogError, "minimum attempt"):
            self.load(fixture)

    def test_exact_c4_cross_field_and_fallback_identity_adversaries_fail_closed(self) -> None:
        fixture = self.build_fixture(cold_failures_override=999, warm_failures_override=999)
        with self.assertRaisesRegex(
            CatalogError, "measurements are incomplete or relabeled|aggregate does not reproduce"
        ):
            self.load(fixture)

        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        item = overlay["promotions"]["proteinmpnn-upstream-portable"]
        item["warm_cohort_digest"] = item["cold_cohort_digest"]
        fixture[3].write_text(json.dumps(overlay) + "\n")
        with self.assertRaisesRegex(CatalogError, "cohort subjects must be distinct"):
            self.load(fixture)

        fixture = self.build_fixture(supply_repository="foreign.example.invalid/other/model")
        with self.assertRaisesRegex(CatalogError, "source differs from static discovery"):
            self.load(fixture)

        fixture = self.build_fixture(
            supply_revision_url="https://foreign.example.invalid/revision/not-the-pin"
        )
        with self.assertRaisesRegex(CatalogError, "source differs from static discovery"):
            self.load(fixture)

        fixture = self.build_fixture(supply_file_count_delta=999)
        with self.assertRaisesRegex(
            CatalogError, "does not bind the full ordered manifest|artifact differs"
        ):
            self.load(fixture)

        fixture = self.build_fixture(supply_bytes_delta=999)
        with self.assertRaisesRegex(
            CatalogError, "does not bind the full ordered manifest|artifact differs"
        ):
            self.load(fixture)

        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        item = overlay["promotions"]["proteinmpnn-upstream-portable"]
        item["candidate_id"] = "diffdock-upstream-v1-1"
        fixture[3].write_text(json.dumps(overlay) + "\n")
        with self.assertRaisesRegex(CatalogError, "another static candidate/profile"):
            self.load(fixture)

        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        item = overlay["promotions"]["proteinmpnn-upstream-portable"]
        interchangeable = digest("interchangeable-subject")
        for field in (
            "artifact_manifest_digest",
            "supply_receipt_digest",
            "runtime_tuple_digest",
            "cold_cohort_digest",
            "warm_cohort_digest",
            "qualification_receipt_digest",
            "independent_review_receipt_digest",
        ):
            item[field] = interchangeable
        fixture[3].write_text(json.dumps(overlay) + "\n")
        with self.assertRaisesRegex(CatalogError, "cohort subjects must be distinct|missing"):
            self.load(fixture)

        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        item = overlay["promotions"]["proteinmpnn-upstream-portable"]
        item["runtime_profile"] = "blackwell-sm103"
        fixture[3].write_text(json.dumps(overlay) + "\n")
        with self.assertRaisesRegex(CatalogError, "another static candidate/profile"):
            self.load(fixture)

    def test_signed_subject_tampering_untrusted_stale_and_replay_fail_closed(self) -> None:
        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        manifest_digest = overlay["promotions"]["proteinmpnn-upstream-portable"][
            "artifact_manifest_digest"
        ]
        path = fixture[4] / "artifacts" / f"{manifest_digest}.json"
        manifest = json.loads(path.read_text())
        manifest["content"]["files"][0]["sha256"], manifest["content"]["files"][1][
            "sha256"
        ] = (
            manifest["content"]["files"][1]["sha256"],
            manifest["content"]["files"][0]["sha256"],
        )
        path.write_text(json.dumps(manifest) + "\n")
        with self.assertRaisesRegex(CatalogError, "content digest|filename/digest binding"):
            self.load(fixture)

        fixture = self.build_fixture()
        untrusted = Ed25519PrivateKey.generate().public_key()
        fixture = (*fixture[:-1], {public_key_id(untrusted): public_key_value(untrusted)})
        with self.assertRaisesRegex(CatalogError, "canonically trusted|untrusted"):
            self.load(fixture)

        fixture = self.build_fixture()
        _, catalog, bindings, overlay, evidence, trust = fixture
        with self.assertRaisesRegex(CatalogError, "not fresh|enabled, ready, fresh"):
            load_variant_gateway_catalog(
                catalog,
                bindings,
                overlay,
                evidence_root=evidence,
                trusted_attestors=trust,
                trusted_attestor_policy=self.trust_policy(),
                validation_time=datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc),
            )

        fixture = self.build_fixture(replay_semantic=True)
        with self.assertRaisesRegex(CatalogError, "replayed"):
            self.load(fixture)

        fixture = self.build_fixture(independent_review=False)
        with self.assertRaisesRegex(CatalogError, "foreign role|independent reviewer"):
            self.load(fixture)
    def test_exact_05_chronology_pairing_supply_lifecycle_and_readiness_fail_closed(self) -> None:
        fixture = self.build_fixture(overlap_attempts=True)
        with self.assertRaisesRegex(CatalogError, "overlap|chronologically"):
            self.load(fixture)

        fixture = self.build_fixture(duplicate_cross_cohort_attempt_id=True)
        with self.assertRaisesRegex(CatalogError, "globally unique"):
            self.load(fixture)

        fixture = self.build_fixture(semantic_attested_early=True)
        with self.assertRaisesRegex(CatalogError, "attested before observation"):
            self.load(fixture)

        fixture = self.build_fixture(preemption_cross_pair=True)
        with self.assertRaisesRegex(CatalogError, "attempt/semantic pair"):
            self.load(fixture)

        fixture = self.build_fixture(
            supply_subject_image_override="foreign.invalid/x@sha256:"
            + digest("foreign-image")
        )
        with self.assertRaisesRegex(CatalogError, "another OCI repository or image"):
            self.load(fixture)

        fixture = self.build_fixture(binding_ready=False)
        with self.assertRaisesRegex(CatalogError, "enabled, ready, fresh serving binding"):
            self.load(fixture)

        fixture = self.build_fixture(review_uses_semantic_signer=True)
        with self.assertRaisesRegex(CatalogError, "foreign role|independent reviewer"):
            self.load(fixture)

        fixture = self.build_fixture()
        overlay = json.loads(fixture[3].read_text())
        item = overlay["promotions"]["proteinmpnn-upstream-portable"]
        (
            fixture[4]
            / "variant-lifecycles"
            / f"{item['zero_to_ready_receipt_digest']}.json"
        ).unlink()
        with self.assertRaisesRegex(CatalogError, "evidence is absent"):
            self.load(fixture)

        fixture = self.build_fixture()
        bad_policy = self.trust_policy()
        evidence_key = public_key_id(self.private_key.public_key())
        assert isinstance(bad_policy["principals"], dict)
        bad_policy["principals"]["review"]["key_id"] = evidence_key
        bad_policy["principals"]["review"]["public_key"] = public_key_value(
            self.private_key.public_key()
        )
        with self.assertRaisesRegex(CatalogError, "distinct|role group"):
            load_variant_gateway_catalog(
                fixture[1],
                fixture[2],
                fixture[3],
                evidence_root=fixture[4],
                trusted_attestors=fixture[5],
                trusted_attestor_policy=bad_policy,
                validation_time=self.validation_time,
            )

    def test_exact_a1_raw_supply_api_preemption_cold_and_role_adversaries(self) -> None:
        for fixture, pattern in (
            (self.build_fixture(invalid_dsse_signature=True), "DSSE signature"),
            (
                self.build_fixture(slsa_repository_override="https://foreign.invalid/source"),
                "SLSA provenance",
            ),
            (
                self.build_fixture(
                    readiness_owner_uid_override="99999999-9999-9999-9999-999999999999"
                ),
                "EndpointSlice",
            ),
            (
                self.build_fixture(
                    readiness_pod_image_override="foreign.invalid/runtime@sha256:"
                    + digest("foreign-runtime")
                ),
                "target Pod",
            ),
            (
                self.build_fixture(
                    readiness_probe_pod_override="99999999-9999-9999-9999-999999999999"
                ),
                "probe",
            ),
            (
                self.build_fixture(
                    preemption_old_node_override="99999999-9999-9999-9999-999999999999"
                ),
                "old identity",
            ),
            (self.build_fixture(preemption_replacement_same=True), "distinct"),
            (self.build_fixture(cold_nonzero_boundary=True), "zero/Pod absence"),
            (self.build_fixture(reuse_cold_pod_uid=True), "distinct new Pod UID"),
            (self.build_fixture(quality_candidate_value=2.0), "closed bound"),
            (
                self.build_fixture(quality_candidate_value=10**1000),
                "finite numeric|closed bound|integer exceeds",
            ),
            (
                self.build_fixture(activation_observed_at="2026-08-27T21:00:03Z"),
                "activate-ready-measure-deactivate",
            ),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(CatalogError, pattern):
                    self.load(fixture)

        fixture = self.build_fixture()
        _, catalog, bindings, overlay, evidence, trust = fixture
        with self.assertRaisesRegex(CatalogError, "fresh|expired"):
            load_variant_gateway_catalog(
                catalog,
                bindings,
                overlay,
                evidence_root=evidence,
                trusted_attestors=trust,
                trusted_attestor_policy=self.trust_policy(),
                validation_time=datetime(2026, 8, 27, 23, 0, 0, tzinfo=timezone.utc),
            )

        fixture = self.build_fixture()
        overlay_value = json.loads(fixture[3].read_text())
        supply_digest = overlay_value["promotions"]["proteinmpnn-upstream-portable"][
            "supply_receipt_digest"
        ]
        supply_value = json.loads(
            (fixture[4] / "variant-supplies" / f"{supply_digest}.json").read_text()
        )
        license_digest = supply_value["license"]["artifact_sha256"]
        (fixture[4] / "variant-license-artifacts" / f"{license_digest}.bin").unlink()
        with self.assertRaisesRegex(CatalogError, "absent.*variant-license-artifacts"):
            self.load(fixture)

        self.role_keys["cohort"] = self.role_keys["semantic"]
        fixture = self.build_fixture()
        with self.assertRaisesRegex(CatalogError, "distinct principals"):
            self.load(fixture)

    def test_variant_contract_fixture_is_stable_and_zero_route_by_default(self) -> None:
        fixture = variant_promotion_contract_fixture()
        self.assertEqual(
            fixture,
            json.loads(
                (
                    CATALOG_ROOT
                    / "contracts"
                    / "model-variant-consumer.fixture.json"
                ).read_text()
            ),
        )
        self.assertFalse(fixture["static_route_authority"])
        self.assertEqual(
            "fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog",
            fixture["loader"],
        )
        self.assertIn("normal-serving-binding-digest", fixture["route_intersection"])
        self.assertIn("service_origin", fixture["public_projection_omits"])


if __name__ == "__main__":
    unittest.main()
