from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fs2_serve_catalog.artifacts import (
    build_artifact_manifest,
    canonical_bytes,
    load_artifact_manifest,
)
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
    verify_signed_attestation,
)
from fs2_serve_catalog.consumer import (
    activation_intent_binding_digest,
    bind_gateway_catalog,
    contract_fixture,
    identity_map,
    load_serving_bindings,
)
from fs2_serve_catalog.evidence import (
    EvidenceStore,
    _gateway_claims,
    _gateway_path,
    _readiness_path_identity,
    _validate_nim_cache_readiness,
    load_faststart_job_admission,
    load_protected_storage_class_admission,
    load_provider_block_writer_admission,
)
from fs2_serve_catalog.loader import (
    CatalogError,
    execution_identity,
    load_catalog,
    resource_placement_identity,
)
from tests.test_ngc_fixtures import make_ngc_materialization


CATALOG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CATALOG_ROOT / "packaged-repository"


class GatewayConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.attestor = Ed25519PrivateKey.generate()
        self.trusted_attestors = {
            public_key_id(self.attestor.public_key()): public_key_value(
                self.attestor.public_key()
            )
        }
        self.session_id = hashlib.sha256(b"fs2-unit-test-evidence-session").hexdigest()
        self.validation_time = datetime(2026, 8, 26, 22, 20, 10, tzinfo=timezone.utc)

    def copy_catalog(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "catalog"
        shutil.copytree(CATALOG_ROOT, target)
        return temporary, target

    def promote_qwen(self, target: Path, manifest_digest: str):
        path = target / "models" / "qwen3-8b.json"
        value = json.loads(path.read_text())
        expected_identity = value["cache"]["artifact"]["expected_identity"]
        # Promotion fixtures always bind the reviewed exact-revision manifest;
        # caller-provided synthetic digests must never become Qwen evidence.
        manifest_digest = expected_identity["manifest_digest"]
        value["model"]["source"]["license"].update(
            {"id": "apache-2.0", "state": "verified", "notes": "Test qualification fixture."}
        )
        image = value["runtime"]["image"]
        image["reference"] = "registry.example.invalid/fs2/qwen@" + image["digest"]
        image["state"] = "resolved"
        value["resources"]["gpu"]["b300_state"] = "qualified"
        placement = value["resources"]["gpu"]["placement"]
        placement["provider_block_pvc"]["state"] = "qualified"
        placement["cache_capabilities"] = ["provider-block-pvc-qualified"]
        placement["qualification_sequence"][0]["state"] = "qualified"
        value["cache"]["artifact"].update(
            {
                "state": "platform-verified",
                "manifest_digest": manifest_digest,
                "expanded_bytes": expected_identity["expanded_bytes"],
                "minimum_bytes": expected_identity["expanded_bytes"],
                "capacity_bound_bytes": 34_359_738_368,
                "staged": False,
            }
        )
        value["support"]["state"] = "qualified"
        value["support"]["route_exposed"] = True
        value["interface"]["mcp"]["invocable"] = True
        path.write_text(json.dumps(value) + "\n")
        return value

    def refresh_scale_contract(self, target: Path, model_id: str) -> None:
        model = json.loads((target / "models" / f"{model_id}.json").read_text())
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        contract_path = target / index["scale_contracts"]["path"]
        document = json.loads(contract_path.read_text())
        item = document["contracts"][model_id]
        model_digest = hashlib.sha256(canonical_bytes(model)).hexdigest()
        executable_digest = execution_identity(model)
        item["model_digest"] = model_digest
        item["execution_identity_sha256"] = executable_digest
        item["resource_placement_identity_sha256"] = resource_placement_identity(model)
        target_value = item["target"]
        if target_value is not None:
            subject = {
                "api_version": target_value["api_version"],
                "kind": target_value["kind"],
                "namespace": target_value["namespace"],
                "name": target_value["name"],
                "selector": target_value["selector"],
                "model_digest": model_digest,
                "execution_identity_sha256": executable_digest,
                "resource_placement_identity_sha256": resource_placement_identity(
                    model
                ),
            }
            target_value["template_identity_sha256"] = hashlib.sha256(
                canonical_bytes(subject)
            ).hexdigest()
        contract_path.write_text(json.dumps(document) + "\n")
        index["scale_contracts"]["sha256"] = hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")

    def binding_value(
        self,
        catalog,
        qualification: dict[str, object] | None = None,
        *,
        enabled: bool = True,
    ) -> dict[str, object]:
        record = catalog.model("qwen3-8b")
        service_identity = {
            "namespace": "fs2-models",
            "service_name": "qwen3-8b",
            "port": 8000,
            "origin": "http://qwen3-8b.fs2-models.svc.cluster.local:8000",
        }
        backend = {
            "class": "local-kubernetes",
            "inventory_model_id": None,
            "region": "us-north1",
            "gpu_class": record.to_dict()["resources"]["gpu"]["class"],
            "runtime_image_digest": record.to_dict()["runtime"]["image"]["digest"],
            "endpoint_identity_sha256": hashlib.sha256(
                canonical_bytes(service_identity)
            ).hexdigest(),
            "trust_bundle_sha256": hashlib.sha256(
                b"unit-test-cluster-trust-bundle"
            ).hexdigest(),
            "credential_requirement_id": None,
        }
        gateway_service_subject = {
            "class": "fs2-serve-gateway",
            "namespace": "fs2-system",
            "service_name": "fs2-serve-control-plane",
            "service_uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "port": 8080,
        }
        empty = {
            "storage_mode": None,
            "artifact_manifest_digest": None,
            "artifact_uri": None,
            "acquisition_receipt_digest": None,
            "prerequisite_receipt_digest": None,
            "target_node_canary_digest": None,
            "placement_receipt_digest": None,
            "runtime_tuple_digest": None,
            "prepared_qualification_digest": None,
            "new_node_qualification_digest": None,
            "semantic_evidence_digest": None,
            "readiness_evidence_digest": None,
            "backend_evidence_digest": None,
            "federated_qualification_digest": None,
            "evidence_session_id": None,
        }
        activation_receipt_keys = {
            "activation_zero_to_ready_receipt_digest",
            "activation_return_to_zero_receipt_digest",
        }
        route_qualification = (
            {
                key: item
                for key, item in qualification.items()
                if key not in activation_receipt_keys
            }
            if qualification is not None
            else None
        )
        return {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {
                "qwen3-8b": {
                    "model_digest": record.digest,
                    "enabled": enabled,
                    "ready": enabled,
                    "valid_until": "2026-08-26T22:20:30Z" if enabled else None,
                    "service": {
                        "execution_mode": "http",
                        "namespace": "fs2-models",
                        "name": "qwen3-8b",
                        "port": 8000,
                        "origin": "http://qwen3-8b.fs2-models.svc.cluster.local:8000",
                        "protocols": ["openai-chat"],
                        "endpoints": {"openai-chat": "/v1/chat/completions"},
                    },
                    "backend": backend,
                    "gateway": {
                        **gateway_service_subject,
                        "identity_sha256": hashlib.sha256(
                            canonical_bytes(gateway_service_subject)
                        ).hexdigest(),
                        "auth_class": "scoped-api-key",
                        "route_id": "qwen3-8b",
                    },
                    "activation": self.activation_value(
                        catalog,
                        "qwen3-8b",
                        enabled=enabled,
                        zero_to_ready_receipt_digest=(
                            None
                            if qualification is None
                            else qualification.get(
                                "activation_zero_to_ready_receipt_digest"
                            )
                        ),
                        return_to_zero_receipt_digest=(
                            None
                            if qualification is None
                            else qualification.get(
                                "activation_return_to_zero_receipt_digest"
                            )
                        ),
                    ),
                    "policy": {"operations": ["chat"]},
                    "mcp": {"enabled": enabled, "tool_name": "qwen3_8b", "description": "Qualified test binding."},
                    "qualification": route_qualification if enabled else empty,
                }
            },
        }

    def activation_value(
        self,
        catalog,
        model_id: str,
        *,
        enabled: bool,
        zero_to_ready_receipt_digest: object = None,
        return_to_zero_receipt_digest: object = None,
    ) -> dict[str, object]:
        scale = catalog.scale_contract(model_id)
        contract_target = scale.to_dict()["target"]
        intent_interface = scale.to_dict()["controller_boundary"][
            "activation_intent_interface"
        ]
        intent_interface_digest = hashlib.sha256(
            canonical_bytes(intent_interface)
        ).hexdigest()
        database_grants_digest = hashlib.sha256(
            canonical_bytes(intent_interface["database_principals"])
        ).hexdigest()
        activation_store = scale.to_dict()["controller_boundary"]["activation_store"]
        activation_store_digest = hashlib.sha256(
            canonical_bytes(activation_store)
        ).hexdigest()
        submitter_database_secret = (
            {
                "namespace": "fs2-system",
                "name": "fs2-activation-submitter-db",
                "uid": "adadadad-adad-adad-adad-adadadadadad",
                "resource_version": "91",
                "type": "Opaque",
                "key_set": ["dsn"],
            }
            if enabled
            else None
        )
        claim_owner_database_secret = (
            {
                "namespace": "fs2-system",
                "name": "fs2-activation-claim-owner-db",
                "uid": "aeaeaeae-aeae-aeae-aeae-aeaeaeaeaeae",
                "resource_version": "92",
                "type": "Opaque",
                "key_set": ["dsn"],
            }
            if enabled
            else None
        )
        controller_subject = {
            "class": "fs2-model-activation-controller",
            "namespace": "fs2-system",
            "deployment_name": "fs2-serve-control-plane-activation",
            "deployment_uid": (
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" if enabled else None
            ),
            "pod_name": "fs2-serve-control-plane-activation-0" if enabled else None,
            "pod_uid": (
                "bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc" if enabled else None
            ),
            "pod_owner_deployment_uid": (
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" if enabled else None
            ),
            "service_account_name": "fs2-model-activation-controller",
            "service_account_uid": (
                "cccccccc-cccc-cccc-cccc-cccccccccccc" if enabled else None
            ),
            "leader_lease_name": "fs2-serve-activation-controller",
            "leader_lease_uid": (
                "abababab-abab-abab-abab-abababababab" if enabled else None
            ),
            "leader_lease_resource_version": "88" if enabled else None,
            "leader_lease_holder_identity": (
                "fs2-activation-controller-a" if enabled else None
            ),
            "leader_lease_renew_time": (
                "2026-08-26T22:20:00Z" if enabled else None
            ),
            "leader_lease_duration_seconds": 30 if enabled else None,
            "leader_role_namespace": "fs2-system",
            "leader_role_name": "fs2-serve-control-plane-activation-leader",
            "target_role_namespace": "fs2-models",
            "target_role_name": "fs2-serve-control-plane-activation-targets",
            "submitter_service_account_name": "fs2-serve-control-plane",
            "submitter_service_account_uid": (
                "acacacac-acac-acac-acac-acacacacacac" if enabled else None
            ),
            "submitter_deployment_name": "fs2-serve-control-plane",
            "submitter_deployment_uid": (
                "afafafaf-afaf-afaf-afaf-afafafafafaf" if enabled else None
            ),
            "submitter_pod_name": "fs2-serve-control-plane-0" if enabled else None,
            "submitter_pod_uid": (
                "babababa-baba-baba-baba-babababababa" if enabled else None
            ),
            "submitter_pod_owner_deployment_uid": (
                "afafafaf-afaf-afaf-afaf-afafafafafaf" if enabled else None
            ),
            "submitter_database_role": "fs2_activation_submitter",
            "claim_owner_database_role": "fs2_activation_claim_owner",
            "submitter_database_secret": submitter_database_secret,
            "claim_owner_database_secret": claim_owner_database_secret,
            "database_grants_sha256": database_grants_digest,
            "activation_store_sha256": activation_store_digest,
            "activation_store_ddl_sha256": activation_store["ddl"]["sha256"],
            "auth_class": (
                "postgres-role-grants-plus-projected-ksa-lease-operation-fence"
            ),
            "intent_interface_sha256": intent_interface_digest,
        }
        controller = {
            **controller_subject,
            "identity_sha256": (
                hashlib.sha256(canonical_bytes(controller_subject)).hexdigest()
                if enabled
                else None
            ),
        }
        target = None
        if contract_target is not None:
            target = {
                key: contract_target[key]
                for key in (
                    "api_version",
                    "kind",
                    "namespace",
                    "name",
                    "template_identity_sha256",
                )
            }
            target.update(
                {
                    "uid": (
                        "dddddddd-dddd-dddd-dddd-dddddddddddd" if enabled else None
                    ),
                    "resource_version": "102" if enabled else None,
                    "observed_generation": 8 if enabled else None,
                }
            )
        return {
            "enabled": enabled,
            "scale_contract_digest": scale.digest,
            "controller": controller,
            "target": target,
            "zero_to_ready_receipt_digest": zero_to_ready_receipt_digest,
            "return_to_zero_receipt_digest": return_to_zero_receipt_digest,
        }

    def write_scale_lifecycle_receipts(
        self,
        evidence: Path,
        catalog,
        *,
        model_id: str,
        runtime_tuple_digest: str,
        artifact_manifest_digest: str,
        content_uri: str,
        binding_digest: str,
    ) -> tuple[str, str]:
        """Create one signed zero-to-ready/return-to-zero fixture pair."""

        record = catalog.model(model_id)
        scale = catalog.scale_contract(model_id)
        activation = self.activation_value(catalog, model_id, enabled=True)
        controller = dict(activation["controller"])
        return_target = dict(activation["target"])
        zero_target = {
            **return_target,
            "resource_version": "101",
            "observed_generation": 7,
        }
        activation_store_digest = controller["activation_store_sha256"]
        zero_intent = {
            "schema": "fs2-serve.nebius.ai/postgres-activation-intent/v3",
            "intent_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": "11111111-1111-4111-8111-111111111111",
            "operation_attempt": 1,
            "fence_operation_id": "11111111-1111-4111-8111-111111111111",
            "model_id": model_id,
            "model_revision": record.to_dict()["model"]["source"]["revision"],
            "binding_digest": binding_digest,
            "action": "activate",
            "store_contract_sha256": activation_store_digest,
            "submitter_service_account_uid": controller[
                "submitter_service_account_uid"
            ],
            "submitter_database_role": controller["submitter_database_role"],
            "claim_owner_service_account_uid": controller["service_account_uid"],
            "claim_owner_database_role": controller["claim_owner_database_role"],
            "controller_id": controller["leader_lease_holder_identity"],
            "previous_fencing_token": 18,
            "fencing_token": 19,
            "database_now": "2026-08-26T22:11:01Z",
            "claim_started_at": "2026-08-26T22:11:01Z",
            "leader_lease_uid": controller["leader_lease_uid"],
            "leader_lease_resource_version": controller[
                "leader_lease_resource_version"
            ],
            "leader_lease_holder_identity": controller[
                "leader_lease_holder_identity"
            ],
            "claim_lease_expires_at": "2026-08-26T22:12:30Z",
        }
        zero_intent["subject_sha256"] = hashlib.sha256(
            canonical_bytes(
                {
                    "intent_id": zero_intent["intent_id"],
                    "operation_id": zero_intent["operation_id"],
                    "operation_attempt": zero_intent["operation_attempt"],
                    "model_id": zero_intent["model_id"],
                    "model_revision": zero_intent["model_revision"],
                    "binding_digest": zero_intent["binding_digest"],
                    "action": zero_intent["action"],
                    "submitter_service_account_uid": zero_intent[
                        "submitter_service_account_uid"
                    ],
                    "store_contract_sha256": zero_intent[
                        "store_contract_sha256"
                    ],
                }
            )
        ).hexdigest()
        zero_replicas = {"previous": 0, "desired": 1, "observed": 1}
        zero_timestamps = {
            "accepted_at": "2026-08-26T22:11:00Z",
            "mutation_at": "2026-08-26T22:11:10Z",
            "ready_at": "2026-08-26T22:12:00Z",
            "duration_seconds": 60.0,
        }
        readiness_contract = scale.to_dict()["readiness"]
        readiness = {
            "method": readiness_contract["method"],
            "path": readiness_contract["path"],
            "expected_status": readiness_contract["expected_status"],
            "observed_status": readiness_contract["expected_status"],
            "checked_at": zero_timestamps["ready_at"],
        }
        replica_ownership = {
            "schema": "fs2-serve.nebius.ai/replica-field-ownership-receipt/v1",
            "api_server_identity_sha256": hashlib.sha256(
                b"unit-test-api-server"
            ).hexdigest(),
            "target_uid": zero_target["uid"],
            "target_resource_version": zero_target["resource_version"],
            "managed_fields_resource_version": zero_target["resource_version"],
            "managed_fields_observed_at": zero_timestamps["ready_at"],
            "replica_field_manager": "fs2-model-activation-controller",
            "replica_field_path": "f:spec/f:replicas",
            "fields_v1_sha256": hashlib.sha256(b"unit-test-managed-fields").hexdigest(),
            "gitops_manager": "argocd-application-controller",
            "gitops_owns_replicas": False,
            "foreign_replica_managers": [],
            "ownership_annotation_sha256": hashlib.sha256(
                b"unit-test-replica-ownership-annotation"
            ).hexdigest(),
        }
        mounted_path = (
            "/mnt/fs2-provider-block"
            + content_uri.removeprefix("pvc://fs2-models/qwen3-8b-weights")
        )
        runtime_startup = {
            "schema": "fs2-serve.nebius.ai/runtime-startup-receipt/v1",
            "model_id": model_id,
            "artifact_manifest_digest": artifact_manifest_digest,
            "artifact_uri": content_uri,
            "mounted_content_path": mounted_path,
            "effective_argv": [
                mounted_path if item == "{FS2_MODEL_CONTENT_PATH}" else item
                for item in record.to_dict()["runtime"]["command"]
            ],
            "served_model_alias": model_id,
            "pod": {
                "api_version": "v1",
                "kind": "Pod",
                "namespace": "fs2-models",
                "name": f"{model_id}-unit-test-pod",
                "uid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "owner_uid": zero_target["uid"],
                "service_account_uid": "edededed-eded-eded-eded-edededededed",
                "container_name": "model",
                "container_id": "containerd://" + hashlib.sha256(b"runtime").hexdigest(),
                "runtime_image_digest": record.to_dict()["runtime"]["image"]["digest"],
            },
            "network_policy": {
                "api_version": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "namespace": "fs2-models",
                "name": f"{model_id}-runtime-deny-egress",
                "uid": "ecececec-ecec-ecec-ecec-ecececececec",
                "resource_version": "100",
                "pod_selector": {
                    "matchLabels": {"fs2-serve.nebius.ai/model-id": model_id}
                },
                "policy_types": ["Egress"],
                "egress": [],
                "observed_at": "2026-08-26T22:11:05Z",
            },
            "network_probe": {
                "dns": "blocked",
                "https": "blocked",
                "model_registry": "blocked",
                "observed_at": "2026-08-26T22:11:55Z",
            },
            "timestamps": {
                "policy_observed_at": "2026-08-26T22:11:05Z",
                "process_started_at": "2026-08-26T22:11:10Z",
                "ready_at": zero_timestamps["ready_at"],
            },
        }
        zero_digest, _ = self.write_receipt(
            evidence,
            "zero-to-ready",
            {
                "schema": "fs2-serve.nebius.ai/zero-to-ready-receipt/v5",
                "status": "PASS",
                "observed_at": zero_timestamps["ready_at"],
                "model_id": model_id,
                "model_digest": record.digest,
                "scale_contract_digest": scale.digest,
                "runtime_tuple_digest": runtime_tuple_digest,
                "intent": zero_intent,
                "controller": controller,
                "target": zero_target,
                "replicas": zero_replicas,
                "replica_ownership": replica_ownership,
                "runtime_startup": runtime_startup,
                "timestamps": zero_timestamps,
                "readiness": readiness,
                "warmup": {
                    "required": False,
                    "status": "not-required",
                    "checked_at": None,
                },
                "preemption": {
                    "notice_observed": False,
                    "new_admissions": "allow",
                    "attempt_outcome": "PASS",
                },
            },
            claims={
                "model_digest": record.digest,
                "scale_contract_digest": scale.digest,
                "runtime_tuple_digest": runtime_tuple_digest,
                "activation_intent_sha256": hashlib.sha256(
                    canonical_bytes(zero_intent)
                ).hexdigest(),
                "operation_id": zero_intent["operation_id"],
                "operation_attempt": zero_intent["operation_attempt"],
                "fence_operation_id": zero_intent["fence_operation_id"],
                "intent_model_id": zero_intent["model_id"],
                "binding_digest": zero_intent["binding_digest"],
                "controller_id": zero_intent["controller_id"],
                "previous_fencing_token": zero_intent["previous_fencing_token"],
                "fencing_token": zero_intent["fencing_token"],
                "database_now": zero_intent["database_now"],
                "claim_started_at": zero_intent["claim_started_at"],
                "intent_subject_sha256": zero_intent["subject_sha256"],
                "activation_store_sha256": zero_intent["store_contract_sha256"],
                "submitter_service_account_uid": zero_intent[
                    "submitter_service_account_uid"
                ],
                "claim_owner_service_account_uid": zero_intent[
                    "claim_owner_service_account_uid"
                ],
                "leader_lease_uid": zero_intent["leader_lease_uid"],
                "leader_lease_resource_version": zero_intent[
                    "leader_lease_resource_version"
                ],
                "leader_lease_holder_identity": zero_intent[
                    "leader_lease_holder_identity"
                ],
                "claim_lease_expires_at": zero_intent["claim_lease_expires_at"],
                "controller_identity_sha256": controller["identity_sha256"],
                "target_identity_sha256": hashlib.sha256(
                    canonical_bytes(zero_target)
                ).hexdigest(),
                "replica_transition_sha256": hashlib.sha256(
                    canonical_bytes(zero_replicas)
                ).hexdigest(),
                "replica_ownership_sha256": hashlib.sha256(
                    canonical_bytes(replica_ownership)
                ).hexdigest(),
                "runtime_startup_sha256": hashlib.sha256(
                    canonical_bytes(runtime_startup)
                ).hexdigest(),
                "lifecycle_timestamps_sha256": hashlib.sha256(
                    canonical_bytes(zero_timestamps)
                ).hexdigest(),
                "readiness_observation_sha256": hashlib.sha256(
                    canonical_bytes(readiness)
                ).hexdigest(),
            },
        )

        expected_resources = [
            {
                "api_version": return_target["api_version"],
                "kind": return_target["kind"],
                "namespace": return_target["namespace"],
                "name": return_target["name"],
                "uid": return_target["uid"],
            },
            {
                "api_version": "v1",
                "kind": "Pod",
                "namespace": "fs2-models",
                "name": f"{model_id}-unit-test-pod",
                "uid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            },
        ]
        expected_resources.sort(
            key=lambda item: (
                item["api_version"],
                item["kind"],
                item["namespace"],
                item["name"],
                item["uid"],
            )
        )
        resources = [
            {
                **resource,
                "precondition_uid": resource["uid"],
                "final_state": (
                    "retained-scaled-zero"
                    if resource["uid"] == return_target["uid"]
                    else "absent"
                ),
            }
            for resource in expected_resources
        ]
        return_replicas = {"previous": 1, "desired": 0, "observed": 0}
        return_timestamps = {
            "last_activity_at": "2026-08-26T22:13:00Z",
            "cooldown_elapsed_at": "2026-08-26T22:18:00Z",
            "drain_started_at": "2026-08-26T22:18:00Z",
            "mutation_at": "2026-08-26T22:18:10Z",
            "zero_observed_at": "2026-08-26T22:19:00Z",
            "duration_seconds": 60.0,
        }
        drain = {
            "new_admissions_stopped": True,
            "active_assignments_before": 0,
            "active_assignments_after": 0,
            "preemption_notice_sha256": None,
            "interrupted_attempt_ids": [],
        }
        retained = [artifact_manifest_digest]
        return_intent = {
            "schema": "fs2-serve.nebius.ai/postgres-activation-intent/v3",
            "intent_id": "22222222-2222-4222-8222-222222222222",
            "operation_id": None,
            "operation_attempt": 0,
            "fence_operation_id": "22222222-2222-4222-8222-222222222222",
            "model_id": model_id,
            "model_revision": record.to_dict()["model"]["source"]["revision"],
            "binding_digest": binding_digest,
            "action": "deactivate",
            "store_contract_sha256": activation_store_digest,
            "submitter_service_account_uid": controller[
                "submitter_service_account_uid"
            ],
            "submitter_database_role": controller["submitter_database_role"],
            "claim_owner_service_account_uid": controller["service_account_uid"],
            "claim_owner_database_role": controller["claim_owner_database_role"],
            "controller_id": controller["leader_lease_holder_identity"],
            "previous_fencing_token": 19,
            "fencing_token": 20,
            "database_now": "2026-08-26T22:18:01Z",
            "claim_started_at": "2026-08-26T22:18:01Z",
            "leader_lease_uid": controller["leader_lease_uid"],
            "leader_lease_resource_version": controller[
                "leader_lease_resource_version"
            ],
            "leader_lease_holder_identity": controller[
                "leader_lease_holder_identity"
            ],
            "claim_lease_expires_at": "2026-08-26T22:19:30Z",
        }
        return_intent["subject_sha256"] = hashlib.sha256(
            canonical_bytes(
                {
                    "intent_id": return_intent["intent_id"],
                    "operation_id": return_intent["operation_id"],
                    "operation_attempt": return_intent["operation_attempt"],
                    "model_id": return_intent["model_id"],
                    "model_revision": return_intent["model_revision"],
                    "binding_digest": return_intent["binding_digest"],
                    "action": return_intent["action"],
                    "submitter_service_account_uid": return_intent[
                        "submitter_service_account_uid"
                    ],
                    "store_contract_sha256": return_intent[
                        "store_contract_sha256"
                    ],
                }
            )
        ).hexdigest()
        return_replica_ownership = {
            **replica_ownership,
            "target_resource_version": return_target["resource_version"],
            "managed_fields_resource_version": return_target["resource_version"],
            "managed_fields_observed_at": return_timestamps["zero_observed_at"],
        }
        return_digest, _ = self.write_receipt(
            evidence,
            "return-to-zero",
            {
                "schema": "fs2-serve.nebius.ai/return-to-zero-receipt/v5",
                "status": "PASS",
                "observed_at": return_timestamps["zero_observed_at"],
                "model_id": model_id,
                "model_digest": record.digest,
                "scale_contract_digest": scale.digest,
                "runtime_tuple_digest": runtime_tuple_digest,
                "intent": return_intent,
                "controller": controller,
                "target": return_target,
                "replicas": return_replicas,
                "replica_ownership": return_replica_ownership,
                "timestamps": return_timestamps,
                "drain": drain,
                "cleanup": {
                    "expected_resource_uids": expected_resources,
                    "resources": resources,
                    "foreign_uids_touched": False,
                    "gpu_clients_after": 0,
                    "temporary_paths_absent": True,
                    "retained_artifact_digests": retained,
                },
            },
            claims={
                "model_digest": record.digest,
                "scale_contract_digest": scale.digest,
                "runtime_tuple_digest": runtime_tuple_digest,
                "activation_intent_sha256": hashlib.sha256(
                    canonical_bytes(return_intent)
                ).hexdigest(),
                "operation_id": return_intent["operation_id"],
                "operation_attempt": return_intent["operation_attempt"],
                "fence_operation_id": return_intent["fence_operation_id"],
                "intent_model_id": return_intent["model_id"],
                "binding_digest": return_intent["binding_digest"],
                "controller_id": return_intent["controller_id"],
                "previous_fencing_token": return_intent["previous_fencing_token"],
                "fencing_token": return_intent["fencing_token"],
                "database_now": return_intent["database_now"],
                "claim_started_at": return_intent["claim_started_at"],
                "intent_subject_sha256": return_intent["subject_sha256"],
                "activation_store_sha256": return_intent["store_contract_sha256"],
                "submitter_service_account_uid": return_intent[
                    "submitter_service_account_uid"
                ],
                "claim_owner_service_account_uid": return_intent[
                    "claim_owner_service_account_uid"
                ],
                "leader_lease_uid": return_intent["leader_lease_uid"],
                "leader_lease_resource_version": return_intent[
                    "leader_lease_resource_version"
                ],
                "leader_lease_holder_identity": return_intent[
                    "leader_lease_holder_identity"
                ],
                "claim_lease_expires_at": return_intent["claim_lease_expires_at"],
                "controller_identity_sha256": controller["identity_sha256"],
                "target_identity_sha256": hashlib.sha256(
                    canonical_bytes(return_target)
                ).hexdigest(),
                "replica_transition_sha256": hashlib.sha256(
                    canonical_bytes(return_replicas)
                ).hexdigest(),
                "replica_ownership_sha256": hashlib.sha256(
                    canonical_bytes(return_replica_ownership)
                ).hexdigest(),
                "lifecycle_timestamps_sha256": hashlib.sha256(
                    canonical_bytes(return_timestamps)
                ).hexdigest(),
                "drain_sha256": hashlib.sha256(canonical_bytes(drain)).hexdigest(),
                "expected_resource_uid_set_sha256": hashlib.sha256(
                    canonical_bytes(expected_resources)
                ).hexdigest(),
                "resource_result_set_sha256": hashlib.sha256(
                    canonical_bytes(resources)
                ).hexdigest(),
                "retained_artifact_set_sha256": hashlib.sha256(
                    canonical_bytes(retained)
                ).hexdigest(),
            },
        )
        return zero_digest, return_digest

    def write_binding(self, target: Path, value: dict[str, object]) -> Path:
        path = target / "serving-bindings.json"
        path.write_text(json.dumps(value) + "\n")
        return path

    @staticmethod
    def secure_evidence_tree(root: Path) -> None:
        """Give generated evidence fixtures the production custody permissions."""
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
            path.chmod(0o750 if path.is_dir() else 0o640)
        root.chmod(0o700)

    def load_live_bindings(self, path: Path, catalog, evidence: Path):
        self.secure_evidence_tree(evidence)
        return load_serving_bindings(
            path,
            catalog,
            evidence_root=evidence,
            trusted_attestors=self.trusted_attestors,
            validation_time=self.validation_time,
        )

    def write_receipt(
        self,
        root: Path,
        kind: str,
        unsigned: dict[str, object],
        *,
        claims: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        value = dict(unsigned)
        value["receipt_digest"] = digest
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{digest}.json").write_text(json.dumps(value) + "\n")
        self.write_attestation(
            root,
            kind,
            digest,
            str(unsigned["schema"]),
            str(unsigned["model_id"]),
            claims,
        )
        return digest, value

    def write_attestation(
        self,
        root: Path,
        kind: str,
        digest: str,
        subject_schema: str,
        model_id: str,
        claims: dict[str, object],
        *,
        session_id: str | None = None,
        private_key: Ed25519PrivateKey | None = None,
        issued_at: str = "2026-08-26T22:20:00Z",
        expires_at: str = "2026-08-26T23:00:00Z",
    ) -> Path:
        nonce = hashlib.sha256(
            f"{kind}:{digest}:{len(list(root.rglob('*.attestation-count')))}".encode()
        ).hexdigest()
        marker = root / f"{nonce}.attestation-count"
        marker.touch()
        attestation = create_signed_attestation(
            private_key=private_key or self.attestor,
            session_id=session_id or self.session_id,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
            kind=kind,
            subject_schema=subject_schema,
            subject_digest=digest,
            model_id=model_id,
            claims=claims,
        )
        directory = root / "attestations" / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        path.write_text(json.dumps(attestation) + "\n")
        return path

    def replace_attestation(
        self,
        path: Path,
        *,
        model_id: str | None = None,
        session_id: str | None = None,
        claims: dict[str, object] | None = None,
        private_key: Ed25519PrivateKey | None = None,
        issued_at: str = "2026-08-26T22:20:00Z",
        expires_at: str = "2026-08-26T23:00:00Z",
    ) -> None:
        old = json.loads(path.read_text())
        subject = old["subject"]
        replacement = create_signed_attestation(
            private_key=private_key or self.attestor,
            session_id=session_id or self.session_id,
            nonce=hashlib.sha256((old["nonce"] + ":replacement").encode()).hexdigest(),
            issued_at=issued_at,
            expires_at=expires_at,
            kind=subject["kind"],
            subject_schema=subject["schema"],
            subject_digest=subject["digest"],
            model_id=model_id or subject["model_id"],
            claims=claims if claims is not None else old["claims"],
        )
        path.write_text(json.dumps(replacement) + "\n")

    def live_fixture(
        self, target: Path, *, storage_mode: str = "provider-block-pvc"
    ) -> tuple[object, Path, dict[str, object], Path]:
        if storage_mode != "provider-block-pvc":
            raise AssertionError("only the provider-block Qwen route is qualified")
        evidence = target / "live-evidence"
        base_qwen = json.loads((target / "models" / "qwen3-8b.json").read_text())
        expected = base_qwen["cache"]["artifact"]["expected_identity"]
        manifest_value = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": "qwen3-8b",
            "kind": "weights",
            "source": {
                "uri": "hf://Qwen/Qwen3-8B",
                "revision": "b968826d9c46dd6066d109eabc6255188de91218",
            },
            "content": {
                "digest": expected["content_digest"],
                "expanded_bytes": expected["expanded_bytes"],
                "files": [
                    {key: item[key] for key in ("path", "bytes", "sha256")}
                    for item in expected["files"]
                ],
            },
            "license": {"id": "apache-2.0", "state": "verified"},
            "entitlement_state": "not-required",
            "owner": "fs2-serve-localizer",
            "retention": "retained-platform",
        }
        (evidence / "artifacts").mkdir(parents=True)
        manifest_path = evidence / "artifacts" / f"{expected['manifest_digest']}.json"
        manifest_path.write_text(json.dumps(manifest_value) + "\n")
        manifest = load_artifact_manifest(manifest_path)
        self.assertEqual(expected["manifest_digest"], manifest.digest)
        self.write_attestation(
            evidence,
            "artifacts",
            manifest.digest,
            "fs2-serve.nebius.ai/artifact-manifest/v1",
            "qwen3-8b",
            {
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "artifact_kind": manifest.kind,
                "model_revision": manifest.source_revision,
            },
        )
        value = self.promote_qwen(target, manifest.digest)
        (target / "models" / "qwen3-8b.json").write_text(json.dumps(value) + "\n")
        self.refresh_scale_contract(target, "qwen3-8b")
        catalog = load_catalog(target, repo_root=REPO_ROOT)

        plan = catalog.acquisition_plan("qwen3-8b")
        plan_digest = hashlib.sha256(canonical_bytes(plan.to_dict())).hexdigest()
        helper_contract = plan.to_dict()["helper_image"]
        helper_contract_digest = hashlib.sha256(
            canonical_bytes(helper_contract)
        ).hexdigest()
        helper_image_digest = "sha256:" + hashlib.sha256(
            b"unit-acquisition-helper-image"
        ).hexdigest()
        helper_image = {
            "id": "fs2-acquisition-helper",
            "reference": (
                "registry.example.invalid/fs2-serve/acquisition-helper@"
                + helper_image_digest
            ),
            "digest": helper_image_digest,
            "registry_identity_sha256": hashlib.sha256(
                b"unit-acquisition-helper-registry"
            ).hexdigest(),
            "os": "linux",
            "architecture": "amd64",
        }
        helper_build = {
            "repository": helper_contract["build_source"]["repository"],
            "source_commit": hashlib.sha1(b"unit-helper-source-commit").hexdigest(),
            "source_tree": hashlib.sha1(b"unit-helper-source-tree").hexdigest(),
            "source_path": helper_contract["build_source"]["path"],
            "package": helper_contract["build_source"]["package"],
            "package_version": helper_contract["build_source"]["package_version"],
            "wheel_sha256": hashlib.sha256(b"unit-helper-wheel").hexdigest(),
            "pyproject_sha256": helper_contract["build_source"]["pyproject_sha256"],
            "uv_lock_sha256": helper_contract["build_source"]["uv_lock_sha256"],
            "entrypoint": helper_contract["entrypoint"],
        }
        helper_attestations = {
            "signature": {
                "verified": True,
                "subject_image_digest": helper_image_digest,
                "registry_identity_sha256": helper_image[
                    "registry_identity_sha256"
                ],
                "bundle_sha256": hashlib.sha256(b"unit-helper-signature").hexdigest(),
                "signer_identity_sha256": hashlib.sha256(b"unit-helper-signer").hexdigest(),
            },
            "provenance": {
                "predicate_type": "https://slsa.dev/provenance/v1",
                "statement_sha256": hashlib.sha256(b"unit-helper-provenance").hexdigest(),
                "subject_image_digest": helper_image_digest,
                "source_commit": helper_build["source_commit"],
                "source_tree": helper_build["source_tree"],
                "build_identity_sha256": hashlib.sha256(
                    canonical_bytes(helper_build)
                ).hexdigest(),
                "helper_contract_sha256": helper_contract_digest,
                "builder_identity_sha256": hashlib.sha256(
                    b"unit-helper-builder"
                ).hexdigest(),
                "build_type": "https://fs2-serve.nebius.ai/build/container/v1",
                "materials_sha256": hashlib.sha256(
                    b"unit-helper-materials"
                ).hexdigest(),
                "all_container_images_digest_pinned": True,
            },
            "sbom": {
                "predicate_type": "https://spdx.dev/Document",
                "statement_sha256": hashlib.sha256(b"unit-helper-sbom").hexdigest(),
                "subject_image_digest": helper_image_digest,
                "package": helper_build["package"],
                "package_version": helper_build["package_version"],
                "wheel_sha256": helper_build["wheel_sha256"],
            },
        }
        helper_review = {
            "review_commit": hashlib.sha1(b"unit-helper-review").hexdigest(),
            "reviewer_identity_sha256": hashlib.sha256(b"unit-helper-reviewer").hexdigest(),
        }
        helper_admission_digest, _ = self.write_receipt(
            evidence,
            "acquisition-helper-images",
            {
                "schema": "fs2-serve.nebius.ai/acquisition-helper-image-admission/v1",
                "status": "PASS",
                "verified_at": "2026-08-26T22:00:00Z",
                "valid_until": "2026-08-26T22:59:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": plan_digest,
                "helper_contract_sha256": helper_contract_digest,
                "image": helper_image,
                "build": helper_build,
                "attestations": helper_attestations,
                "review": helper_review,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": plan_digest,
                "helper_contract_sha256": helper_contract_digest,
                "image_reference": helper_image["reference"],
                "image_digest": helper_image_digest,
                "registry_identity_sha256": helper_image["registry_identity_sha256"],
                "build_identity_sha256": hashlib.sha256(
                    canonical_bytes(helper_build)
                ).hexdigest(),
                "attestation_identity_sha256": hashlib.sha256(
                    canonical_bytes(helper_attestations)
                ).hexdigest(),
                "review_identity_sha256": hashlib.sha256(
                    canonical_bytes(helper_review)
                ).hexdigest(),
            },
        )
        content_uri = (
            "pvc://fs2-models/qwen3-8b-weights/models/qwen3-8b/sha256/"
            + manifest.content_digest
        )
        acquisition_prerequisites = list(plan.required_prerequisite_ids)
        helper_subject = {
            "id": "fs2-acquisition-helper",
            "reference": helper_image["reference"],
            "digest": helper_image_digest,
            "admission_receipt_digest": helper_admission_digest,
            "registry_identity_sha256": helper_image["registry_identity_sha256"],
            "build_identity_sha256": hashlib.sha256(
                canonical_bytes(helper_build)
            ).hexdigest(),
            "helper_contract_sha256": helper_contract_digest,
        }
        acquisition_job = {
            "api_version": "batch/v1",
            "kind": "Job",
            "namespace": "fs2-models",
            "name": "qwen3-8b-cache-unit-test-acquisition",
            "uid": "10101010-1010-1010-1010-101010101010",
        }
        acquisition_pod = {
            "api_version": "v1",
            "kind": "Pod",
            "namespace": "fs2-models",
            "name": "qwen3-8b-cache-unit-test-acquisition-unit00",
            "uid": "20202020-2020-2020-2020-202020202020",
            "owner_job_uid": acquisition_job["uid"],
        }
        acquisition_execution = {
            "run_as_non_root": True,
            "run_as_uid": 10001,
            "run_as_gid": 10001,
            "fs_group": 10001,
            "supplemental_groups_policy": "Strict",
            "seccomp_profile": "RuntimeDefault",
            "job": acquisition_job,
            "pod": acquisition_pod,
        }
        expected_acquisition_resources = [acquisition_job, acquisition_pod]
        acquisition_cleanup = {
            "completed_at": "2026-08-26T22:10:00Z",
            "observer_identity_sha256": hashlib.sha256(
                b"unit-acquisition-cleanup-observer"
            ).hexdigest(),
            "controller_identity_sha256": hashlib.sha256(
                b"unit-acquisition-cleanup-controller"
            ).hexdigest(),
            "api_server_observed": True,
            "expected_resources": expected_acquisition_resources,
            "resources": [
                {
                    **item,
                    "delete_precondition_uid": item["uid"],
                    "final_state": "absent",
                    "replacement_uid": None,
                    "replacement_touched": False,
                }
                for item in expected_acquisition_resources
            ],
            "temporary_path_absent": True,
            "write_marker_absent": True,
            "foreign_uids_touched": False,
        }
        worker_result_digest = hashlib.sha256(
            b"unit-acquisition-worker-result"
        ).hexdigest()
        acquisition_digest, _ = self.write_receipt(
            evidence,
            "acquisition",
            {
                "schema": "fs2-serve.nebius.ai/artifact-acquisition-receipt/v4",
                "worker_result_digest": worker_result_digest,
                "operation_id": "unit-test-acquisition",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "method": "huggingface-public-snapshot",
                "source": {
                    "repository": "Qwen/Qwen3-8B",
                    "revision": "b968826d9c46dd6066d109eabc6255188de91218",
                },
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "content_uri": content_uri,
                "prerequisite_ids": acquisition_prerequisites,
                "storage": {
                    "mode": "provider-block-pvc",
                    "contract": "fs2-serve.nebius.ai/provider-block-pvc/v1",
                    "pvc_namespace": "fs2-models",
                    "pvc_name": "qwen3-8b-weights",
                },
                "credential_source": "none-public-revision",
                "token_used": False,
                "publication": "atomic-content-addressed-provider-block-pvc",
                "controller_owner": "fs2-serve-acquirer",
                "acquisition_plan_sha256": plan_digest,
                "helper_image": helper_subject,
                "execution": acquisition_execution,
                "filesystem_write_proof": {
                    "filesystem_type": "ext4",
                    "probe_path": "/mnt/fs2-provider-block/models/qwen3-8b",
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
                },
                "lock_path": "/mnt/fs2-provider-block/models/.locks/qwen3-8b.acquire.lock",
                "capacity_bound_bytes": catalog.model("qwen3-8b")
                .to_dict()["cache"]["artifact"]["capacity_bound_bytes"],
                "reserve_bytes": 8589934592,
                "free_bytes_before": catalog.model("qwen3-8b")
                .to_dict()["cache"]["artifact"]["capacity_bound_bytes"]
                + 8589934592,
                "free_bytes_after": 8589934592,
                "outcome": "acquired",
                "cleanup": acquisition_cleanup,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": plan_digest,
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "content_uri": content_uri,
                "prerequisite_set_sha256": hashlib.sha256(
                    canonical_bytes(acquisition_prerequisites)
                ).hexdigest(),
                "storage_contract_sha256": hashlib.sha256(
                    canonical_bytes(
                        {
                            "mode": "provider-block-pvc",
                            "contract": "fs2-serve.nebius.ai/provider-block-pvc/v1",
                            "pvc_namespace": "fs2-models",
                            "pvc_name": "qwen3-8b-weights",
                        }
                    )
                ).hexdigest(),
                "execution_identity_sha256": hashlib.sha256(
                    canonical_bytes(acquisition_execution)
                ).hexdigest(),
                "worker_result_digest": worker_result_digest,
                "helper_image_identity_sha256": hashlib.sha256(
                    canonical_bytes(helper_subject)
                ).hexdigest(),
                "cleanup_identity_sha256": hashlib.sha256(
                    canonical_bytes(acquisition_cleanup)
                ).hexdigest(),
                "filesystem_write_proof_sha256": hashlib.sha256(
                    canonical_bytes(
                        {
                            "filesystem_type": "ext4",
                            "probe_path": "/mnt/fs2-provider-block/models/qwen3-8b",
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
                    )
                ).hexdigest(),
            },
        )
        observed_resources = []
        for ordinal, requirement_id in enumerate(acquisition_prerequisites, start=1):
            prerequisite = catalog.prerequisite(requirement_id).to_dict()
            kind = prerequisite["kind"]
            observed_resources.append(
                {
                    "id": requirement_id,
                    "api_version": prerequisite["api_version"],
                    "kind": kind,
                    "namespace": prerequisite["namespace"],
                    "name": prerequisite["name"],
                    "uid": f"{ordinal:08x}-aaaa-bbbb-cccc-{ordinal:012x}",
                    "resource_version": str(ordinal),
                    "state": "Bound" if kind == "PersistentVolumeClaim" else "present",
                    "secret_type": prerequisite["secret_type"],
                    "data_keys": prerequisite["required_keys"],
                    "access_modes": prerequisite["access_modes"],
                    "capacity_bytes": (
                        prerequisite["minimum_capacity_bytes"]
                        if kind == "PersistentVolumeClaim"
                        else None
                    ),
                }
            )
        observation = {
            "schema": "fs2-serve.nebius.ai/observed-prerequisites/v4",
            "values_suppressed": True,
            "legacy_ngc_secret_copied": False,
            "legacy_plaintext_rotation_source_used": False,
            "legacy_phase_7c_hmac_reused": False,
            "exposed_evo_bearer_reused": False,
            "ngc_credential_materialization": None,
            "resources": observed_resources,
        }
        prerequisite_digest, _ = self.write_receipt(
            evidence,
            "prerequisites",
            {
                "schema": "fs2-serve.nebius.ai/runtime-prerequisite-receipt/v4",
                "status": "PASS",
                "checked_at": "2026-08-26T22:00:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": plan_digest,
                "observation": observation,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": plan_digest,
                "resource_identity_set_sha256": hashlib.sha256(
                    canonical_bytes(observed_resources)
                ).hexdigest(),
                "values_suppressed": True,
                "legacy_ngc_secret_copied": False,
                "legacy_plaintext_rotation_source_used": False,
                "legacy_phase_7c_hmac_reused": False,
                "exposed_evo_bearer_reused": False,
                "ngc_credential_materialization_sha256": None,
            },
        )

        acquirer_node = {
            "name": "fs2-b300-unit-node",
            "uid": "99999999-9999-9999-9999-999999999999",
            "provider_id_sha256": hashlib.sha256(b"unit-provider-id").hexdigest(),
        }
        serving_node = {
            "name": "fs2-b300-unit-replacement",
            "uid": "88888888-8888-8888-8888-888888888888",
            "provider_id_sha256": hashlib.sha256(
                b"unit-replacement-provider-id"
            ).hexdigest(),
        }
        placement_receipt_digest = None
        runtime_placement_digest = acquisition_digest
        worker_image_digest = "sha256:" + hashlib.sha256(b"worker-image").hexdigest()
        nvidia_smi_digest = hashlib.sha256(b"nvidia-smi-output").hexdigest()
        plugin_image_digest = "sha256:" + hashlib.sha256(b"device-plugin-image").hexdigest()
        nvme_inventory_digest = hashlib.sha256(b"nvme-inventory").hexdigest()
        gpu_tuple = {
            "class": "NVIDIA-B300-SXM6-288GB",
            "node_preset": "b300-8x",
            "node_count": 8,
            "node_topology": "eight-gpu-nvlink",
            "workload_count": 1,
            "workload_topology": "single-gpu",
            "allocated_uuids": ["GPU-11111111-1111-1111-1111-111111111111"],
            "node_inventory_sha256": hashlib.sha256(
                b"unit-test-eight-b300-inventory"
            ).hexdigest(),
        }
        runtime_claims = {
            "artifact_manifest_digest": manifest.digest,
            "placement_receipt_digest": runtime_placement_digest,
            "worker_image_digest": worker_image_digest,
            "driver_version": "580.173.02",
            "cuda_version": "13.0",
            "device_plugin_image_digest": plugin_image_digest,
            "gpu_tuple_sha256": hashlib.sha256(canonical_bytes(gpu_tuple)).hexdigest(),
            "serving_node_identity_sha256": hashlib.sha256(
                canonical_bytes(serving_node)
            ).hexdigest(),
            "runtime_image_digest": catalog.model("qwen3-8b")
            .to_dict()["runtime"]["image"]["digest"],
            "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "execution_identity_sha256": execution_identity(
                catalog.model("qwen3-8b").to_dict()
            ),
        }
        runtime_digest, _ = self.write_receipt(
            evidence,
            "runtime-tuples",
            {
                "schema": "fs2-serve.nebius.ai/b300-runtime-tuple/v5",
                "status": "verified",
                "captured_at": "2026-08-26T22:00:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "project_id_sha256": "4bbabe330d3a6ca777209264b4407554760c5121f9d0c91d91374394d1697caf",
                "project_alias": "rene-us-north",
                "region": "us-north1",
                "cluster_id_sha256": "b373262d3603551276953acf0c99b4a4bfe6ebb26b5b006e914206399692139c",
                "cluster_alias": "unit-test-fixture",
                "worker": {
                    "image_reference": "nebius.invalid/worker@" + worker_image_digest,
                    "image_digest": worker_image_digest,
                    "nvidia_driver_version": "580.173.02",
                    "nvidia_smi_sha256": nvidia_smi_digest,
                    "cuda_version": "13.0",
                    "device_plugin": {
                        "owner": "nebius-managed-kubernetes",
                        "image_digest": plugin_image_digest,
                        "singleton": True,
                    },
                    "node": serving_node,
                    "node_selector": {
                        "capacity.fs2.nebius/gpu-count": "8",
                        "capacity.fs2.nebius/pool": "burst",
                        "capacity.fs2.nebius/preset": "b300-8x",
                        "capacity.fs2.nebius/type": "preemptible",
                        "workload.fs2.nebius/gpu": "true",
                    },
                    "gpu": gpu_tuple,
                    "nvme_inventory_sha256": nvme_inventory_digest,
                },
                "runtime": {
                    "image_digest": catalog.model("qwen3-8b")
                    .to_dict()["runtime"]["image"]["digest"],
                    "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                    "startup_mechanism": "conventional",
                    "command_sha256": hashlib.sha256(
                        canonical_bytes(
                            catalog.model("qwen3-8b").to_dict()["runtime"]["command"]
                        )
                    ).hexdigest(),
                    "execution_identity_sha256": execution_identity(
                        catalog.model("qwen3-8b").to_dict()
                    ),
                    "checkpoint": None,
                },
                "artifact": {
                    "manifest_digest": manifest.digest,
                    "content_uri": content_uri,
                    "placement_receipt_digest": runtime_placement_digest,
                },
            },
            claims=runtime_claims,
        )
        semantic_identity = catalog.model("qwen3-8b").to_dict()["semantic_validator"]
        semantic_contract = catalog.semantic_request_contract("qwen3-8b")
        validator_subject = {
            key: semantic_identity[key]
            for key in (
                "contract",
                "source_path",
                "source_sha256",
                "fixture_path",
                "fixture_sha256",
            )
        }

        def write_validator_result(
            attempt_id: str,
            responses: list[dict[str, object]],
            gateway_path: dict[str, object] | None = None,
        ) -> str:
            requests = [str(item["request_sha256"]) for item in responses]
            outputs = [str(item["response_sha256"]) for item in responses]
            digest, _ = self.write_receipt(
                evidence,
                "semantic-validations",
                {
                    "schema": "fs2-serve.nebius.ai/semantic-validation-result/v3",
                    "status": "PASS",
                    "validated_at": "2026-08-26T22:05:00Z",
                    "model_id": "qwen3-8b",
                    "model_digest": catalog.model("qwen3-8b").digest,
                    "runtime_identity_digest": runtime_digest,
                    "request_contract_sha256": semantic_contract.digest,
                    "request_asset_set_sha256": semantic_contract.asset_set_digest,
                    "gateway_path_sha256": _gateway_claims(gateway_path)[
                        "gateway_path_sha256"
                    ],
                    "attempt_id": attempt_id,
                    "validator": validator_subject,
                    "request_sha256": requests,
                    "response_sha256": outputs,
                },
                claims={
                    "runtime_identity_digest": runtime_digest,
                    "attempt_id": attempt_id,
                    "request_contract_sha256": semantic_contract.digest,
                    "request_asset_set_sha256": semantic_contract.asset_set_digest,
                    "validator_identity_sha256": hashlib.sha256(
                        canonical_bytes(validator_subject)
                    ).hexdigest(),
                    "request_set_sha256": hashlib.sha256(
                        canonical_bytes(requests)
                    ).hexdigest(),
                    "response_set_sha256": hashlib.sha256(
                        canonical_bytes(outputs)
                    ).hexdigest(),
                    **_gateway_claims(gateway_path),
                },
            )
            return digest

        cohort_digests: dict[str, str] = {}
        for cohort_ordinal, cohort in enumerate(("prepared-node", "new-node"), start=1):
            attempts = []
            for index in range(1, 4):
                receipt_key = f"{cohort}-{index}"
                responses = [
                    {
                        "request_id": semantic_contract.request_ids[0],
                        "request_sha256": semantic_contract.request_sha256[0],
                        "response_sha256": hashlib.sha256(
                            (receipt_key + "-response-first").encode()
                        ).hexdigest(),
                        "semantic_valid": True,
                    },
                    {
                        "request_id": semantic_contract.request_ids[1],
                        "request_sha256": semantic_contract.request_sha256[1],
                        "response_sha256": hashlib.sha256(
                            (receipt_key + "-response-second").encode()
                        ).hexdigest(),
                        "semantic_valid": True,
                    },
                ]
                validator_result_digest = write_validator_result(
                    receipt_key, responses
                )
                semantic_digest, _ = self.write_receipt(
                    evidence,
                    "semantic",
                    {
                        "schema": "fs2-serve.nebius.ai/semantic-receipt/v3",
                        "status": "PASS",
                        "model_id": "qwen3-8b",
                        "model_digest": catalog.model("qwen3-8b").digest,
                        "runtime_tuple_digest": runtime_digest,
                        "request_contract_sha256": semantic_contract.digest,
                        "request_asset_set_sha256": semantic_contract.asset_set_digest,
                        "attempt_id": receipt_key,
                        "observed_at": "2026-08-26T22:05:00Z",
                        "validator": validator_subject,
                        "responses": responses,
                        "distinct_requests": True,
                        "distinct_responses": True,
                        "gateway_path": None,
                        "validator_result_digest": validator_result_digest,
                    },
                    claims={
                        "runtime_tuple_digest": runtime_digest,
                        "attempt_id": receipt_key,
                        "request_contract_sha256": semantic_contract.digest,
                        "request_asset_set_sha256": semantic_contract.asset_set_digest,
                        "validator_identity_sha256": hashlib.sha256(
                            canonical_bytes(validator_subject)
                        ).hexdigest(),
                        "call_set_sha256": hashlib.sha256(
                            canonical_bytes(responses)
                        ).hexdigest(),
                        **_gateway_claims(None),
                        "validator_result_digest": validator_result_digest,
                    },
                )
                receipt_ordinal = cohort_ordinal * 10 + index
                uid = f"{receipt_ordinal:08x}-1111-1111-1111-{receipt_ordinal:012x}"
                resources = [
                    {
                        "api_version": "batch/v1",
                        "kind": "Job",
                        "namespace": "fs2-faststart",
                        "name": receipt_key,
                        "uid": uid,
                        "precondition_uid": uid,
                        "final_state": "absent",
                    }
                ]
                expected_resource_uids = [
                    {
                        key: resources[0][key]
                        for key in ("api_version", "kind", "namespace", "name", "uid")
                    }
                ]
                cleanup_digest, _ = self.write_receipt(
                    evidence,
                    "cleanup",
                    {
                        "schema": "fs2-serve.nebius.ai/uid-cleanup-receipt/v4",
                        "status": "PASS",
                        "model_id": "qwen3-8b",
                        "model_digest": catalog.model("qwen3-8b").digest,
                        "attempt_id": receipt_key,
                        "runtime_tuple_digest": runtime_digest,
                        "completed_at": "2026-08-26T22:10:00Z",
                        "namespace": "fs2-faststart",
                        "node_identity": serving_node,
                        "gpu_identity": gpu_tuple,
                        "expected_resource_uids": expected_resource_uids,
                        "resources": resources,
                        "temporary_paths_absent": True,
                        "gpu_clients_after": 0,
                        "retained_artifact_digests": [manifest.digest],
                    },
                    claims={
                        "attempt_id": receipt_key,
                        "runtime_tuple_digest": runtime_digest,
                        "node_identity_sha256": hashlib.sha256(
                            canonical_bytes(serving_node)
                        ).hexdigest(),
                        "gpu_identity_sha256": hashlib.sha256(
                            canonical_bytes(gpu_tuple)
                        ).hexdigest(),
                        "namespace": "fs2-faststart",
                        "expected_resource_uid_set_sha256": hashlib.sha256(
                            canonical_bytes(expected_resource_uids)
                        ).hexdigest(),
                        "resource_set_sha256": hashlib.sha256(
                            canonical_bytes(resources)
                        ).hexdigest(),
                        "retained_artifact_set_sha256": hashlib.sha256(
                            canonical_bytes([manifest.digest])
                        ).hexdigest(),
                    },
                )
                attempts.append(
                    {
                        "attempt_id": receipt_key,
                        "status": "PASS",
                        "t0_utc": f"2026-08-26T22:0{index}:00Z",
                        "completion_utc": f"2026-08-26T22:0{index}:30Z",
                        "t0_to_call2_seconds": 30.0,
                        "semantic_receipt_digest": semantic_digest,
                        "cleanup_receipt_digest": cleanup_digest,
                        "expected_resource_uids": expected_resource_uids,
                    }
                )
            qualification_unsigned = {
                "schema": "fs2-serve.nebius.ai/qualification-cohort/v4",
                "status": "exploratory-pass",
                "cohort": cohort,
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "startup_mechanism": "conventional",
                "runtime_tuple_digest": runtime_digest,
                "placement_receipt_digest": runtime_placement_digest,
                "attempt_count": 3,
                "success_count": 3,
                "failure_count": 0,
                "attempts": attempts,
                "p50_t0_to_call2_seconds": 30.0,
                "p95_t0_to_call2_seconds": None,
            }
            cohort_digests[cohort], _ = self.write_receipt(
                evidence,
                "qualifications",
                qualification_unsigned,
                claims={
                    "cohort": cohort,
                    "runtime_tuple_digest": runtime_digest,
                    "placement_receipt_digest": runtime_placement_digest,
                    "attempt_set_sha256": hashlib.sha256(
                        canonical_bytes(attempts)
                    ).hexdigest(),
                },
            )
        gateway_attempt = "gateway-smoke"
        service_origin = "http://qwen3-8b.fs2-models.svc.cluster.local:8000"
        service_uid = "22222222-2222-2222-2222-222222222222"
        gateway_service_subject = {
            "class": "fs2-serve-gateway",
            "namespace": "fs2-system",
            "service_name": "fs2-serve-control-plane",
            "service_uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "port": 8080,
        }
        gateway_identity = {
            **gateway_service_subject,
            "identity_sha256": hashlib.sha256(
                canonical_bytes(gateway_service_subject)
            ).hexdigest(),
            "auth_class": "scoped-api-key",
            "route_id": "qwen3-8b",
        }
        backend = {
            "namespace": "fs2-models",
            "service_name": "qwen3-8b",
            "service_uid": service_uid,
            "port": 8000,
            "origin": service_origin,
        }
        binding_backend = {
            "class": "local-kubernetes",
            "inventory_model_id": None,
            "region": "us-north1",
            "gpu_class": "NVIDIA-B300-SXM6-288GB",
            "runtime_image_digest": catalog.model("qwen3-8b")
            .to_dict()["runtime"]["image"]["digest"],
            "endpoint_identity_sha256": hashlib.sha256(
                canonical_bytes(
                    {
                        "namespace": "fs2-models",
                        "service_name": "qwen3-8b",
                        "port": 8000,
                        "origin": service_origin,
                    }
                )
            ).hexdigest(),
            "trust_bundle_sha256": hashlib.sha256(
                b"unit-test-cluster-trust-bundle"
            ).hexdigest(),
            "credential_requirement_id": None,
        }
        backend_subject = {
            **binding_backend,
            "namespace": "fs2-models",
            "service_name": "qwen3-8b",
            "service_uid": service_uid,
            "port": 8000,
            "origin": service_origin,
        }
        credential_subject = {
            "requirement_id": None,
            "secret_uid": None,
            "resource_version": None,
            "rotated_at": None,
            "values_suppressed": True,
        }
        backend_digest, _ = self.write_receipt(
            evidence,
            "backends",
            {
                "schema": "fs2-serve.nebius.ai/backend-identity-receipt/v1",
                "status": "PASS",
                "checked_at": "2026-08-26T22:20:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "backend": backend_subject,
                "credential": credential_subject,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "runtime_image_digest": binding_backend["runtime_image_digest"],
                "backend_identity_sha256": hashlib.sha256(
                    canonical_bytes({**binding_backend, **backend, "service_uid": service_uid})
                ).hexdigest(),
                "credential_identity_sha256": hashlib.sha256(
                    canonical_bytes(credential_subject)
                ).hexdigest(),
            },
        )
        signed_backend = {
            **binding_backend,
            **backend,
            "service_uid": service_uid,
            "observed_generation": 1,
        }
        readiness_digest, _ = self.write_receipt(
            evidence,
            "readiness",
            {
                "schema": "fs2-serve.nebius.ai/readiness-receipt/v2",
                "status": "PASS",
                "checked_at": "2026-08-26T22:20:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "runtime_tuple_digest": runtime_digest,
                "backend": backend,
                "observed_generation": 1,
                "ready_endpoint": "/health",
                "http_status": 200,
            },
            claims={
                "runtime_tuple_digest": runtime_digest,
                "backend_identity_sha256": hashlib.sha256(
                    canonical_bytes(signed_backend)
                ).hexdigest(),
                "readiness_contract_sha256": hashlib.sha256(
                    canonical_bytes({"path": "/health", "expected_status": 200})
                ).hexdigest(),
            },
        )
        readiness_identity = _readiness_path_identity(
            catalog.model("qwen3-8b"),
            evidence_kind="signed-readiness-receipt",
            evidence_digest=readiness_digest,
            service_uid=service_uid,
            observed_generation=1,
            observation=signed_backend,
        )
        gateway_path = _gateway_path(
            catalog.model("qwen3-8b"),
            semantic_contract,
            gateway_identity,
            backend_subject,
            readiness_identity,
        )
        gateway_responses = [
            {
                "request_id": semantic_contract.request_ids[0],
                "request_sha256": semantic_contract.request_sha256[0],
                "response_sha256": hashlib.sha256(b"gateway-response-first").hexdigest(),
                "semantic_valid": True,
            },
            {
                "request_id": semantic_contract.request_ids[1],
                "request_sha256": semantic_contract.request_sha256[1],
                "response_sha256": hashlib.sha256(b"gateway-response-second").hexdigest(),
                "semantic_valid": True,
            },
        ]
        gateway_validator_result = write_validator_result(
            gateway_attempt, gateway_responses, gateway_path
        )
        gateway_semantic_digest, _ = self.write_receipt(
            evidence,
            "semantic",
            {
                "schema": "fs2-serve.nebius.ai/semantic-receipt/v3",
                "status": "PASS",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "runtime_tuple_digest": runtime_digest,
                "request_contract_sha256": semantic_contract.digest,
                "request_asset_set_sha256": semantic_contract.asset_set_digest,
                "attempt_id": gateway_attempt,
                "observed_at": "2026-08-26T22:18:00Z",
                "validator": validator_subject,
                "responses": gateway_responses,
                "distinct_requests": True,
                "distinct_responses": True,
                "gateway_path": gateway_path,
                "validator_result_digest": gateway_validator_result,
            },
            claims={
                "runtime_tuple_digest": runtime_digest,
                "attempt_id": gateway_attempt,
                "request_contract_sha256": semantic_contract.digest,
                "request_asset_set_sha256": semantic_contract.asset_set_digest,
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(validator_subject)
                ).hexdigest(),
                "call_set_sha256": hashlib.sha256(
                    canonical_bytes(gateway_responses)
                ).hexdigest(),
                **_gateway_claims(gateway_path),
                "validator_result_digest": gateway_validator_result,
            },
        )
        qualification = {
            "storage_mode": storage_mode,
            "artifact_manifest_digest": manifest.digest,
            "artifact_uri": content_uri,
            "acquisition_receipt_digest": acquisition_digest,
            "prerequisite_receipt_digest": prerequisite_digest,
            "target_node_canary_digest": None,
            "placement_receipt_digest": placement_receipt_digest,
            "runtime_tuple_digest": runtime_digest,
            "prepared_qualification_digest": cohort_digests["prepared-node"],
            "new_node_qualification_digest": cohort_digests["new-node"],
            "semantic_evidence_digest": gateway_semantic_digest,
            "readiness_evidence_digest": readiness_digest,
            "backend_evidence_digest": backend_digest,
            "federated_qualification_digest": None,
            "evidence_session_id": self.session_id,
        }
        draft_binding = self.binding_value(catalog, qualification)["bindings"][
            "qwen3-8b"
        ]
        binding_digest = activation_intent_binding_digest(draft_binding)
        zero_digest, return_digest = self.write_scale_lifecycle_receipts(
            evidence,
            catalog,
            model_id="qwen3-8b",
            runtime_tuple_digest=runtime_digest,
            artifact_manifest_digest=manifest.digest,
            content_uri=content_uri,
            binding_digest=binding_digest,
        )
        qualification.update(
            {
                "activation_zero_to_ready_receipt_digest": zero_digest,
                "activation_return_to_zero_receipt_digest": return_digest,
            }
        )
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
            "cluster_identity_sha256": hashlib.sha256(
                b"unit-cluster-identity"
            ).hexdigest(),
            "api_server_identity_sha256": hashlib.sha256(
                b"unit-apiserver-identity"
            ).hexdigest(),
            "service_account_namespace": "fs2-system",
            "service_account_name": "fs2-storage-contract-observer",
            "service_account_uid": "12121212-1212-1212-1212-121212121212",
        }
        intended_claim = {
            "namespace": "fs2-models",
            "name": "qwen3-8b-weights",
            "model_id": "qwen3-8b",
            "model_digest": catalog.model("qwen3-8b").digest,
        }
        storage_class_admission_digest, _ = self.write_receipt(
            evidence,
            "protected-storage-classes",
            {
                "schema": "fs2-serve.nebius.ai/protected-storage-class-receipt/v1",
                "status": "PASS",
                "observed_at": "2026-08-26T21:45:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "observer": observer,
                "storage_class": storage_class,
                "intended_claim": intended_claim,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
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
        )
        claim = {
            "namespace": "fs2-models",
            "name": "qwen3-8b-weights",
            "uid": "44444444-4444-4444-4444-444444444444",
            "resource_version": "19",
            "volume_name": "pvc-44444444-4444-4444-4444-444444444444",
            "capacity_bytes": 68_719_476_736,
            "access_modes": ["ReadWriteOnce"],
            "volume_mode": "Filesystem",
            "phase": "Bound",
            "bound_at": "2026-08-26T21:51:00Z",
            "bound_after_acquirer_scheduled": True,
        }
        acquirer = {
            "namespace": "fs2-models",
            "name": "qwen3-8b-cache-qwen-acquire-1",
            "uid": "33333333-3333-3333-3333-333333333333",
            "resource_version": "7",
            "scheduled_at": "2026-08-26T21:50:00Z",
            "completed_at": "2026-08-26T21:55:00Z",
            "node": acquirer_node,
            "node_selector": catalog.model("qwen3-8b")
            .to_dict()["resources"]["gpu"]["placement"]["node_selector"],
            "tolerations": catalog.model("qwen3-8b")
            .to_dict()["resources"]["gpu"]["placement"]["tolerations"],
            "gpu_count": 0,
            "first_consumer": True,
            "sole_writer": True,
            "phase": "Succeeded",
        }
        writer_controller_subject = {
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
        writer_controller = {
            **writer_controller_subject,
            "identity_sha256": hashlib.sha256(
                canonical_bytes(writer_controller_subject)
            ).hexdigest(),
        }
        admitted_claim = {
            "namespace": "fs2-models",
            "name": "qwen3-8b-weights",
            "uid": claim["uid"],
            "resource_version": "17",
        }
        admitted_writer = {
            "api_version": "batch/v1",
            "kind": "Job",
            "namespace": "fs2-models",
            "name": acquirer["name"],
            "service_account_name": "cache-service-account",
            "service_account_uid": "00000005-aaaa-bbbb-cccc-000000000005",
        }
        writer_lease = {
            "api_version": "coordination.k8s.io/v1",
            "kind": "Lease",
            "namespace": "fs2-models",
            "name": "qwen3-8b-weights-writer",
            "uid": "16161616-1616-1616-1616-161616161616",
            "resource_version": "18",
            "holder_identity": (
                "qwen-acquire-1:qwen3-8b-cache-qwen-acquire-1"
            ),
            "fencing_token": 23,
            "renew_time": "2026-08-26T21:49:00Z",
            "lease_duration_seconds": 900,
        }
        writer_mount_set = {
            "api_server_identity_sha256": observer[
                "api_server_identity_sha256"
            ],
            "namespace": "fs2-models",
            "claim_uid": admitted_claim["uid"],
            "list_resource_version": "16",
            "continue_token": None,
            "remaining_item_count": 0,
            "complete": True,
            "observed_at": "2026-08-26T21:49:20Z",
            "mounts": [],
        }
        writer_mount_set_digest = hashlib.sha256(
            canonical_bytes(writer_mount_set)
        ).hexdigest()
        writer_api_fence = {
            "enforcement": (
                "controller-owned-job-create-plus-validating-admission-policy-plus-lease-cas"
            ),
            "api_server_applied": True,
            "claim_resource_version": admitted_claim["resource_version"],
            "allowed_operation_id": "qwen-acquire-1",
            "allowed_writer_name": acquirer["name"],
            "allowed_creator_service_account_uid": writer_controller[
                "service_account_uid"
            ],
            "lease_uid": writer_lease["uid"],
            "fencing_token": writer_lease["fencing_token"],
            "complete_mount_set_sha256": writer_mount_set_digest,
            "writer_create_role_uid": writer_controller["writer_create_role_uid"],
            "writer_create_role_binding_uid": writer_controller[
                "writer_create_role_binding_uid"
            ],
            "race_window": (
                "closed-by-controller-held-lease-through-job-create-and-completion"
            ),
            "second_writer_denied": True,
        }
        writer_admission_digest, _ = self.write_receipt(
            evidence,
            "provider-block-writer-admissions",
            {
                "schema": "fs2-serve.nebius.ai/provider-block-writer-admission/v2",
                "status": "admitted",
                "admitted_at": "2026-08-26T21:49:30Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "operation_id": "qwen-acquire-1",
                "storage_class_receipt_digest": storage_class_admission_digest,
                "claim": admitted_claim,
                "writer": admitted_writer,
                "controller": writer_controller,
                "lease": writer_lease,
                "mount_set": writer_mount_set,
                "api_fence": writer_api_fence,
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "storage_class_receipt_digest": storage_class_admission_digest,
                "operation_id": "qwen-acquire-1",
                "claim_identity_sha256": hashlib.sha256(
                    canonical_bytes(admitted_claim)
                ).hexdigest(),
                "writer_identity_sha256": hashlib.sha256(
                    canonical_bytes(admitted_writer)
                ).hexdigest(),
                "controller_identity_sha256": writer_controller[
                    "identity_sha256"
                ],
                "lease_identity_sha256": hashlib.sha256(
                    canonical_bytes(writer_lease)
                ).hexdigest(),
                "complete_mount_set_sha256": writer_mount_set_digest,
                "api_fence_sha256": hashlib.sha256(
                    canonical_bytes(writer_api_fence)
                ).hexdigest(),
            },
        )
        writer_fence = {
            "controller_identity_sha256": writer_controller["identity_sha256"],
            "writer_admission_complete_mount_set_sha256": writer_mount_set_digest,
            "lease": {
                "uid": writer_lease["uid"],
                "resource_version": writer_lease["resource_version"],
                "holder_identity": writer_lease["holder_identity"],
                "fencing_token": writer_lease["fencing_token"],
            },
            "api_observation": {
                "observed_at": "2026-08-26T21:49:45Z",
                "api_server_identity_sha256": observer[
                    "api_server_identity_sha256"
                ],
                "claim_uid": claim["uid"],
                "claim_resource_version": claim["resource_version"],
                "active_writer_uids": [acquirer["uid"]],
                "denied_second_writer_request_uid": (
                    "17171717-1717-1717-1717-171717171717"
                ),
                "denial_reason": "fs2-provider-block-sole-writer-fence-conflict",
            },
        }
        replacement = {
            "controlled": True,
            "original_node": acquirer_node,
            "replacement_node": serving_node,
            "detached_at": "2026-08-26T22:00:00Z",
            "attached_at": "2026-08-26T22:10:00Z",
            "no_multi_attach": True,
            "attach_attempts": 1,
            "manifest_reverified": True,
        }
        provider_digest, _ = self.write_receipt(
            evidence,
            "provider-block-pvc",
            {
                "schema": "fs2-serve.nebius.ai/provider-block-pvc-lifecycle-receipt/v4",
                "status": "PASS",
                "observed_at": "2026-08-26T22:19:00Z",
                "model_id": "qwen3-8b",
                "model_digest": catalog.model("qwen3-8b").digest,
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "content_uri": content_uri,
                "acquisition_receipt_digest": acquisition_digest,
                "storage_class_admission_receipt_digest": (
                    storage_class_admission_digest
                ),
                "writer_admission_receipt_digest": writer_admission_digest,
                "storage_class": storage_class,
                "claim": claim,
                "acquirer_job": acquirer,
                "writer_fence": writer_fence,
                "handoff": {
                    "closed_at": "2026-08-26T21:56:00Z",
                    "writer_admission_receipt_digest": writer_admission_digest,
                    "writer_admission_complete_mount_set_sha256": writer_mount_set_digest,
                    "no_active_writers": True,
                    "active_writer_uids": [],
                    "sole_writer_uid": acquirer["uid"],
                    "payload_read_only_after_handoff": True,
                    "runtime_read_only_admitted": True,
                    "lease_uid": writer_lease["uid"],
                    "lease_resource_version_after_release": "21",
                    "lease_holder_identity_after_release": None,
                    "released_fencing_token": writer_lease["fencing_token"],
                    "api_server_observed": True,
                },
                "replacement": replacement,
                "runtime": {
                    "deployment_namespace": "fs2-models",
                    "deployment_name": "qwen3-8b",
                    "deployment_uid": "66666666-6666-6666-6666-666666666666",
                    "runtime_tuple_digest": runtime_digest,
                    "node": serving_node,
                    "pvc_read_only": True,
                    "gpu_count": 1,
                    "semantic_receipt_digest": gateway_semantic_digest,
                },
                "scale_to_zero": {
                    "from_replicas": 1,
                    "to_replicas": 0,
                    "return_to_zero_receipt_digest": return_digest,
                    "claim_retained": True,
                    "claim_state": "Bound",
                    "no_active_writers": True,
                },
            },
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "content_uri": content_uri,
                "acquisition_receipt_digest": acquisition_digest,
                "storage_class_admission_receipt_digest": (
                    storage_class_admission_digest
                ),
                "writer_admission_receipt_digest": writer_admission_digest,
                "storage_class_identity_sha256": hashlib.sha256(
                    canonical_bytes(storage_class)
                ).hexdigest(),
                "claim_identity_sha256": hashlib.sha256(
                    canonical_bytes(claim)
                ).hexdigest(),
                "acquirer_job_identity_sha256": hashlib.sha256(
                    canonical_bytes(acquirer)
                ).hexdigest(),
                "writer_fence_identity_sha256": hashlib.sha256(
                    canonical_bytes(writer_fence)
                ).hexdigest(),
                "handoff_identity_sha256": hashlib.sha256(
                    canonical_bytes(
                        {
                            "closed_at": "2026-08-26T21:56:00Z",
                            "writer_admission_receipt_digest": writer_admission_digest,
                            "writer_admission_complete_mount_set_sha256": writer_mount_set_digest,
                            "no_active_writers": True,
                            "active_writer_uids": [],
                            "sole_writer_uid": acquirer["uid"],
                            "payload_read_only_after_handoff": True,
                            "runtime_read_only_admitted": True,
                            "lease_uid": writer_lease["uid"],
                            "lease_resource_version_after_release": "21",
                            "lease_holder_identity_after_release": None,
                            "released_fencing_token": writer_lease[
                                "fencing_token"
                            ],
                            "api_server_observed": True,
                        }
                    )
                ).hexdigest(),
                "replacement_identity_sha256": hashlib.sha256(
                    canonical_bytes(replacement)
                ).hexdigest(),
                "runtime_tuple_digest": runtime_digest,
                "runtime_node_identity_sha256": hashlib.sha256(
                    canonical_bytes(serving_node)
                ).hexdigest(),
                "semantic_receipt_digest": gateway_semantic_digest,
                "return_to_zero_receipt_digest": return_digest,
            },
        )
        qualification["placement_receipt_digest"] = provider_digest
        return catalog, evidence, qualification, manifest.path

    def federated_molmim_fixture(
        self, target: Path
    ) -> tuple[object, Path, dict[str, object], dict[str, object]]:
        evidence = target / "federated-evidence"
        source = target / "federated-source"
        source.mkdir()
        (source / "nim-cache.index").write_bytes(b"qualified-molmim-upstream-cache")
        manifest = build_artifact_manifest(
            source,
            model_id="molmim",
            kind="nim-cache",
            source_uri="ngc://nvcr.io/nim/nvidia/molmim",
            source_revision="sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa",
            license_id="NVIDIA-AI-Enterprise",
            license_state="verified",
            entitlement_state="verified",
            owner="qualified-federated-backend",
            retention="retained-platform",
        )
        model_path = target / "models" / "molmim.json"
        model = json.loads(model_path.read_text())
        model["model"]["source"]["license"].update(
            {
                "id": "NVIDIA-AI-Enterprise",
                "state": "verified",
                "notes": "Unit-test reviewed license fixture.",
            }
        )
        model["model"]["source"]["entitlement"].update(
            {"state": "verified", "notes": "Unit-test scoped entitlement fixture."}
        )
        model["cache"]["artifact"].update(
            {
                "state": "platform-verified",
                "manifest_digest": manifest.digest,
                "expanded_bytes": manifest.expanded_bytes,
                "minimum_bytes": manifest.expanded_bytes,
            }
        )
        model["support"].update(
            {
                "state": "qualified",
                "route_exposed": True,
                "limitations": [
                    "Unit-test exact SM90 upstream; local B300 scheduling remains forbidden."
                ],
            }
        )
        model["interface"]["mcp"]["invocable"] = True
        model_path.write_text(json.dumps(model) + "\n")

        endpoint_identity = hashlib.sha256(b"qualified-molmim-upstream-endpoint").hexdigest()
        trust_bundle = hashlib.sha256(b"qualified-molmim-upstream-trust").hexdigest()
        federation_path = target / "contracts" / "federated-backends.json"
        federation = json.loads(federation_path.read_text())
        inventory = federation["records"]["molmim"]
        inventory.update(
            {
                "backend_state": "qualified",
                "route_state": "qualified",
                "trust_state": "verified",
                "endpoint_identity_sha256": endpoint_identity,
                "trust_bundle_sha256": trust_bundle,
            }
        )
        federation_path.write_text(json.dumps(federation) + "\n")
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        index["federated_backends"]["sha256"] = hashlib.sha256(
            federation_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")
        self.refresh_scale_contract(target, "molmim")
        catalog = load_catalog(target, repo_root=REPO_ROOT)
        record = catalog.model("molmim")

        (evidence / "artifacts").mkdir(parents=True)
        (evidence / "artifacts" / f"{manifest.digest}.json").write_text(
            json.dumps(manifest.to_dict()) + "\n"
        )
        self.write_attestation(
            evidence,
            "artifacts",
            manifest.digest,
            "fs2-serve.nebius.ai/artifact-manifest/v1",
            "molmim",
            {
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "artifact_kind": manifest.kind,
                "model_revision": manifest.source_revision,
            },
        )

        service_origin = "http://molmim.fs2-models.svc.cluster.local:8000"
        service_uid = "33333333-3333-3333-3333-333333333333"
        binding_backend = {
            "class": "federated-kserve-nim",
            "inventory_model_id": "molmim",
            "region": "us-central1",
            "gpu_class": "NVIDIA-H200-SXM",
            "runtime_image_digest": model["runtime"]["image"]["digest"],
            "endpoint_identity_sha256": endpoint_identity,
            "trust_bundle_sha256": trust_bundle,
            "credential_requirement_id": "fs2-models/molmim-upstream-token",
        }
        service_identity = {
            "namespace": "fs2-models",
            "service_name": "molmim",
            "port": 8000,
            "origin": service_origin,
        }
        backend_subject = {
            **binding_backend,
            **service_identity,
            "service_uid": service_uid,
        }
        credential_subject = {
            "requirement_id": "fs2-models/molmim-upstream-token",
            "secret_uid": "44444444-4444-4444-4444-444444444444",
            "resource_version": "17",
            "rotated_at": "2026-08-26T22:10:00Z",
            "values_suppressed": True,
        }
        backend_digest, _ = self.write_receipt(
            evidence,
            "backends",
            {
                "schema": "fs2-serve.nebius.ai/backend-identity-receipt/v1",
                "status": "PASS",
                "checked_at": "2026-08-26T22:20:00Z",
                "model_id": "molmim",
                "model_digest": record.digest,
                "backend": backend_subject,
                "credential": credential_subject,
            },
            claims={
                "model_digest": record.digest,
                "runtime_image_digest": binding_backend["runtime_image_digest"],
                "backend_identity_sha256": hashlib.sha256(
                    canonical_bytes(
                        {**binding_backend, **service_identity, "service_uid": service_uid}
                    )
                ).hexdigest(),
                "credential_identity_sha256": hashlib.sha256(
                    canonical_bytes(credential_subject)
                ).hexdigest(),
            },
        )

        artifact = {
            "manifest_digest": manifest.digest,
            "content_digest": manifest.content_digest,
            "content_uri": (
                f"federated://{endpoint_identity}/models/molmim/sha256/"
                f"{manifest.content_digest}"
            ),
            "staging_state": "ready-on-exact-federated-backend",
        }
        runtime = {
            "image_digest": binding_backend["runtime_image_digest"],
            "model_revision": model["model"]["source"]["revision"],
            "execution_identity_sha256": execution_identity(record.to_dict()),
        }
        readiness = {
            "method": "GET",
            "path": "/v1/health/ready",
            "expected_status": 200,
            "observed_status": 200,
            "observed_at": "2026-08-26T22:18:00Z",
        }
        validator = {
            key: model["semantic_validator"][key]
            for key in (
                "contract",
                "source_path",
                "source_sha256",
                "fixture_path",
                "fixture_sha256",
            )
        }
        semantic_contract = catalog.semantic_request_contract("molmim")
        responses = [
            {
                "request_id": semantic_contract.request_ids[0],
                "request_sha256": semantic_contract.request_sha256[0],
                "response_sha256": hashlib.sha256(b"molmim-response-first").hexdigest(),
                "semantic_valid": True,
            },
            {
                "request_id": semantic_contract.request_ids[1],
                "request_sha256": semantic_contract.request_sha256[1],
                "response_sha256": hashlib.sha256(b"molmim-response-second").hexdigest(),
                "semantic_valid": True,
            },
        ]
        gateway_service_subject = {
            "class": "fs2-serve-gateway",
            "namespace": "fs2-system",
            "service_name": "fs2-serve-control-plane",
            "service_uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "port": 8080,
        }
        gateway_identity = {
            **gateway_service_subject,
            "identity_sha256": hashlib.sha256(
                canonical_bytes(gateway_service_subject)
            ).hexdigest(),
            "auth_class": "scoped-api-key",
            "route_id": "molmim",
        }
        readiness_digest = hashlib.sha256(canonical_bytes(readiness)).hexdigest()
        readiness_identity = _readiness_path_identity(
            record,
            evidence_kind="embedded-federated-readiness",
            evidence_digest=readiness_digest,
            service_uid=service_uid,
            observed_generation=None,
            observation=readiness,
        )
        gateway_path = _gateway_path(
            record,
            semantic_contract,
            gateway_identity,
            backend_subject,
            readiness_identity,
        )
        federated_request_hashes = [item["request_sha256"] for item in responses]
        federated_response_hashes = [item["response_sha256"] for item in responses]
        validator_result_digest, _ = self.write_receipt(
            evidence,
            "semantic-validations",
            {
                "schema": "fs2-serve.nebius.ai/semantic-validation-result/v3",
                "status": "PASS",
                "validated_at": "2026-08-26T22:19:00Z",
                "model_id": "molmim",
                "model_digest": record.digest,
                "runtime_identity_digest": backend_digest,
                "request_contract_sha256": semantic_contract.digest,
                "request_asset_set_sha256": semantic_contract.asset_set_digest,
                "gateway_path_sha256": _gateway_claims(gateway_path)[
                    "gateway_path_sha256"
                ],
                "attempt_id": "gateway-smoke",
                "validator": validator,
                "request_sha256": federated_request_hashes,
                "response_sha256": federated_response_hashes,
            },
            claims={
                "runtime_identity_digest": backend_digest,
                "attempt_id": "gateway-smoke",
                "request_contract_sha256": semantic_contract.digest,
                "request_asset_set_sha256": semantic_contract.asset_set_digest,
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(validator)
                ).hexdigest(),
                "request_set_sha256": hashlib.sha256(
                    canonical_bytes(federated_request_hashes)
                ).hexdigest(),
                "response_set_sha256": hashlib.sha256(
                    canonical_bytes(federated_response_hashes)
                ).hexdigest(),
                **_gateway_claims(gateway_path),
            },
        )
        semantic = {
            "attempt_id": "gateway-smoke",
            "observed_at": "2026-08-26T22:19:00Z",
            "request_contract_sha256": semantic_contract.digest,
            "request_asset_set_sha256": semantic_contract.asset_set_digest,
            "validator": validator,
            "responses": responses,
            "distinct_requests": True,
            "distinct_responses": True,
            "gateway_path": gateway_path,
            "validator_result_digest": validator_result_digest,
        }
        backend_identity = {**service_identity, **binding_backend}
        federated_claims = {
            "model_digest": record.digest,
            "backend_evidence_digest": backend_digest,
            "backend_subject_sha256": hashlib.sha256(
                canonical_bytes(backend_identity)
            ).hexdigest(),
            "artifact_subject_sha256": hashlib.sha256(
                canonical_bytes(artifact)
            ).hexdigest(),
            "runtime_identity_sha256": hashlib.sha256(
                canonical_bytes(runtime)
            ).hexdigest(),
            "readiness_contract_sha256": hashlib.sha256(
                canonical_bytes(
                    {key: readiness[key] for key in ("method", "path", "expected_status")}
                )
            ).hexdigest(),
            "readiness_observation_sha256": hashlib.sha256(
                canonical_bytes(readiness)
            ).hexdigest(),
            "validator_identity_sha256": hashlib.sha256(
                canonical_bytes(validator)
            ).hexdigest(),
            "request_contract_sha256": semantic_contract.digest,
            "request_asset_set_sha256": semantic_contract.asset_set_digest,
            "call_set_sha256": hashlib.sha256(canonical_bytes(responses)).hexdigest(),
            **_gateway_claims(gateway_path),
            "validator_result_digest": validator_result_digest,
        }
        federated_digest, _ = self.write_receipt(
            evidence,
            "federated-qualifications",
            {
                "schema": "fs2-serve.nebius.ai/federated-qualification-receipt/v2",
                "status": "PASS",
                "checked_at": "2026-08-26T22:20:00Z",
                "model_id": "molmim",
                "model_digest": record.digest,
                "backend_evidence_digest": backend_digest,
                "artifact": artifact,
                "runtime": runtime,
                "readiness": readiness,
                "semantic": semantic,
            },
            claims=federated_claims,
        )
        qualification = {
            "storage_mode": None,
            "artifact_manifest_digest": manifest.digest,
            "artifact_uri": artifact["content_uri"],
            "acquisition_receipt_digest": None,
            "prerequisite_receipt_digest": None,
            "target_node_canary_digest": None,
            "placement_receipt_digest": None,
            "runtime_tuple_digest": None,
            "prepared_qualification_digest": None,
            "new_node_qualification_digest": None,
            "semantic_evidence_digest": None,
            "readiness_evidence_digest": None,
            "backend_evidence_digest": backend_digest,
            "federated_qualification_digest": federated_digest,
            "evidence_session_id": self.session_id,
        }
        binding = {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {
                "molmim": {
                    "model_digest": record.digest,
                    "enabled": True,
                    "ready": True,
                    "valid_until": "2026-08-26T23:00:00Z",
                    "service": {
                        "execution_mode": "http",
                        "namespace": "fs2-models",
                        "name": "molmim",
                        "port": 8000,
                        "origin": service_origin,
                        "protocols": ["native"],
                        "endpoints": {"native": "/generate"},
                    },
                    "backend": binding_backend,
                    "gateway": gateway_identity,
                    "activation": self.activation_value(
                        catalog, "molmim", enabled=False
                    ),
                    "policy": {"operations": ["generate-molecule"]},
                    "mcp": {
                        "enabled": True,
                        "tool_name": "molmim",
                        "description": "Qualified exact H200 upstream fixture.",
                    },
                    "qualification": qualification,
                }
            },
        }
        return catalog, evidence, qualification, binding

    def test_contract_fixture_is_exact_and_names_one_canonical_base(self) -> None:
        expected = json.loads((CATALOG_ROOT / "contracts" / "gateway-consumer.fixture.json").read_text())
        self.assertEqual(expected, contract_fixture())
        self.assertEqual("fs2-serve.nebius.ai/model/v1", expected["base_model_schema"])
        self.assertEqual(
            "fs2-serve.nebius.ai/model-variants/v4",
            expected["model_variants_schema"],
        )
        self.assertEqual("none", expected["variant_consumer"]["static_route_authority"])
        self.assertEqual(
            "GatewayModel.binding.activation.intent_interface_sha256",
            expected["activation_consumer"]["intent_interface_field"],
        )
        self.assertNotIn("controller_endpoint", expected["activation_consumer"])
        self.assertNotIn("endpoint_field", expected["activation_consumer"])
        self.assertEqual(
            [
                "class",
                "namespace",
                "deployment_name",
                "deployment_uid",
                "pod_name",
                "pod_uid",
                "pod_owner_deployment_uid",
                "service_account_name",
                "service_account_uid",
                "leader_lease_name",
                "leader_lease_uid",
                "leader_lease_resource_version",
                "leader_lease_holder_identity",
                "leader_lease_renew_time",
                "leader_lease_duration_seconds",
                "leader_role_namespace",
                "leader_role_name",
                "target_role_namespace",
                "target_role_name",
                "submitter_service_account_name",
                "submitter_service_account_uid",
                "submitter_deployment_name",
                "submitter_deployment_uid",
                "submitter_pod_name",
                "submitter_pod_uid",
                "submitter_pod_owner_deployment_uid",
                "submitter_database_role",
                "claim_owner_database_role",
                "submitter_database_secret",
                "claim_owner_database_secret",
                "database_grants_sha256",
                "activation_store_sha256",
                "activation_store_ddl_sha256",
                "auth_class",
                "intent_interface_sha256",
            ],
            expected["activation_consumer"]["controller_subject_fields"],
        )
        self.assertEqual(
            "sha256(canonical GatewayModel.scale_contract.controller_boundary."
            "activation_intent_interface)",
            expected["activation_consumer"]["intent_interface_digest_source"],
        )
        self.assertEqual(
            "PostgresStore.ensure_activation_intent",
            expected["activation_consumer"]["intent_submission"],
        )
        self.assertEqual(
            "forbidden",
            expected["activation_consumer"]["gateway_kubernetes_mutation"],
        )

    def test_golden_identity_map_matches_every_canonical_record(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        expected = json.loads((CATALOG_ROOT / "contracts" / "golden-identities.json").read_text())
        self.assertEqual(expected, identity_map(catalog))
        self.assertEqual(
            "b968826d9c46dd6066d109eabc6255188de91218",
            expected["models"]["qwen3-8b"]["source_revision"],
        )

    def test_base_catalog_is_never_routing_authority(self) -> None:
        _, target = self.copy_catalog()
        self.promote_qwen(target, hashlib.sha256(b"base-manifest").hexdigest())
        self.refresh_scale_contract(target, "qwen3-8b")
        catalog = load_catalog(target, repo_root=REPO_ROOT)
        self.assertEqual((), catalog.routable_model_ids())

    def test_exact_overlay_completes_all_promotion_gates(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        binding_value = self.binding_value(catalog, qualification)
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for overlay validation: {exc}")
        Draft202012Validator(
            json.loads(
                (CATALOG_ROOT / "schema" / "serving-bindings.schema.json").read_text()
            )
        ).validate(binding_value)
        acquisition_receipt = json.loads(
            (
                evidence
                / "acquisition"
                / f"{qualification['acquisition_receipt_digest']}.json"
            ).read_text()
        )
        Draft202012Validator(
            json.loads(
                (
                    CATALOG_ROOT
                    / "schema"
                    / "artifact-acquisition-receipt.schema.json"
                ).read_text()
            )
        ).validate(acquisition_receipt)
        backend_receipt = json.loads(
            (
                evidence
                / "backends"
                / f"{qualification['backend_evidence_digest']}.json"
            ).read_text()
        )
        Draft202012Validator(
            json.loads(
                (CATALOG_ROOT / "schema" / "backend-identity-receipt.schema.json").read_text()
            )
        ).validate(backend_receipt)
        semantic_receipt = json.loads(
            (
                evidence
                / "semantic"
                / f"{qualification['semantic_evidence_digest']}.json"
            ).read_text()
        )
        Draft202012Validator(
            json.loads(
                (CATALOG_ROOT / "schema" / "semantic-receipt.schema.json").read_text()
            )
        ).validate(semantic_receipt)
        validation_receipt = json.loads(
            (
                evidence
                / "semantic-validations"
                / f"{semantic_receipt['validator_result_digest']}.json"
            ).read_text()
        )
        Draft202012Validator(
            json.loads(
                (
                    CATALOG_ROOT
                    / "schema"
                    / "semantic-validation-result.schema.json"
                ).read_text()
            )
        ).validate(validation_receipt)
        prepared = json.loads(
            (
                evidence
                / "qualifications"
                / f"{qualification['prepared_qualification_digest']}.json"
            ).read_text()
        )
        cleanup_digest = prepared["attempts"][0]["cleanup_receipt_digest"]
        schema_subjects = [
            (
                "b300-runtime-tuple.schema.json",
                evidence
                / "runtime-tuples"
                / f"{qualification['runtime_tuple_digest']}.json",
            ),
            (
                "qualification-cohort.schema.json",
                evidence
                / "qualifications"
                / f"{qualification['prepared_qualification_digest']}.json",
            ),
            (
                "uid-cleanup-receipt.schema.json",
                evidence / "cleanup" / f"{cleanup_digest}.json",
            ),
            (
                "zero-to-ready-receipt.schema.json",
                evidence
                / "zero-to-ready"
                / f"{qualification['activation_zero_to_ready_receipt_digest']}.json",
            ),
            (
                "return-to-zero-receipt.schema.json",
                evidence
                / "return-to-zero"
                / f"{qualification['activation_return_to_zero_receipt_digest']}.json",
            ),
        ]
        if qualification["placement_receipt_digest"] is not None:
            schema_subjects.insert(
                0,
                (
                    "provider-block-pvc-lifecycle-receipt.schema.json",
                    evidence
                    / "provider-block-pvc"
                    / f"{qualification['placement_receipt_digest']}.json",
                ),
            )
        for schema_name, subject_path in schema_subjects:
            with self.subTest(schema=schema_name):
                Draft202012Validator(
                    json.loads((CATALOG_ROOT / "schema" / schema_name).read_text())
                ).validate(json.loads(subject_path.read_text()))
        intent_validator = Draft202012Validator(
            json.loads(
                (
                    CATALOG_ROOT
                    / "schema"
                    / "postgres-activation-intent.schema.json"
                ).read_text()
            )
        )
        for kind, digest in (
            ("zero-to-ready", qualification["activation_zero_to_ready_receipt_digest"]),
            ("return-to-zero", qualification["activation_return_to_zero_receipt_digest"]),
        ):
            intent_validator.validate(
                json.loads((evidence / kind / f"{digest}.json").read_text())["intent"]
            )
        binding_path = self.write_binding(target, binding_value)
        bindings = self.load_live_bindings(binding_path, catalog, evidence)
        gateway = bind_gateway_catalog(catalog, bindings)
        self.assertEqual(("qwen3-8b",), gateway.routable_model_ids())
        activation = gateway.model("qwen3-8b").binding.activation
        self.assertEqual(
            "fs2-serve-control-plane-activation",
            activation.controller_deployment_name,
        )
        self.assertEqual(
            "fs2-serve-activation-controller",
            activation.controller_leader_lease_name,
        )
        self.assertEqual(
            "fs2-system",
            activation.controller_leader_role_namespace,
        )
        self.assertEqual(
            "fs2-serve-control-plane-activation-leader",
            activation.controller_leader_role_name,
        )
        self.assertEqual(
            "fs2-models",
            activation.controller_target_role_namespace,
        )
        self.assertEqual(
            "fs2-serve-control-plane-activation-targets",
            activation.controller_target_role_name,
        )
        self.assertFalse(hasattr(activation, "endpoint"))
        self.assertEqual(
            bindings.get("qwen3-8b").binding_digest,
            json.loads(
                (
                    evidence
                    / "zero-to-ready"
                    / f"{qualification['activation_zero_to_ready_receipt_digest']}.json"
                ).read_text()
            )["intent"]["binding_digest"],
        )
        qwen = gateway.model("qwen3-8b").to_dict()
        self.assertTrue(qwen["routable"])
        self.assertEqual("b968826d9c46dd6066d109eabc6255188de91218", qwen["model_revision"])

    def test_qwen_provider_block_route_requires_distinct_lifecycle_placement(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        self.assertEqual("provider-block-pvc", qualification["storage_mode"])
        self.assertIsNotNone(qualification["placement_receipt_digest"])
        self.assertFalse((evidence / "staging").exists())
        binding_path = self.write_binding(
            target, self.binding_value(catalog, qualification)
        )
        gateway = bind_gateway_catalog(
            catalog, self.load_live_bindings(binding_path, catalog, evidence)
        )
        qwen = gateway.model("qwen3-8b").to_dict()
        self.assertTrue(qwen["routable"])
        self.assertEqual("provider-block-pvc", qwen["serving"]["storage_mode"])

    def test_qwen_storage_placement_substitution_fails_closed(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        cases = {
            "provider-with-acquisition-as-placement": lambda value: value.update(
                {"placement_receipt_digest": value["acquisition_receipt_digest"]}
            ),
            "sfs-with-provider-uri": lambda value: value.update(
                {"storage_mode": "sfs-pvc"}
            ),
            "nvme-with-provider-uri": lambda value: value.update(
                {"storage_mode": "local-nvme"}
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                candidate = copy.deepcopy(qualification)
                mutate(candidate)
                path = self.write_binding(
                    target, self.binding_value(catalog, candidate)
                )
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

    def test_provider_block_lifecycle_rejects_storage_handoff_and_node_substitution(self) -> None:
        cases = {
            "default-delete-class": lambda value: value["storage_class"]["spec"].update(
                {"reclaimPolicy": "Delete"}
            ),
            "immediate-binding": lambda value: value["storage_class"]["spec"].update(
                {"volumeBindingMode": "Immediate"}
            ),
            "volume-type-drift": lambda value: value["storage_class"]["spec"][
                "parameters"
            ].update({"type": "NETWORK_SSD_NON_REPLICATED"}),
            "filesystem-parameter-drift": lambda value: value["storage_class"][
                "spec"
            ]["parameters"].update({"csi.storage.k8s.io/fstype": "xfs"}),
            "missing-storage-class-uid": lambda value: value["storage_class"][
                "metadata"
            ].pop("uid"),
            "empty-storage-class-resource-version": lambda value: value[
                "storage_class"
            ]["metadata"].update({"resourceVersion": ""}),
            "caller-semantic-storage-class": lambda value: value.update(
                {
                    "storage_class": {
                        "name": "fs2-network-ssd-retain",
                        "uid": "55555555-5555-5555-5555-555555555555",
                        "resource_version": "11",
                        "provisioner": "compute.csi.nebius.com",
                        "reclaim_policy": "Retain",
                        "volume_binding_mode": "WaitForFirstConsumer",
                        "allow_volume_expansion": True,
                        "volume_type": "NETWORK_SSD",
                        "fs_type": "ext4",
                    }
                }
            ),
            "not-target-bound": lambda value: value["claim"].update(
                {"bound_after_acquirer_scheduled": False}
            ),
            "gpu-acquirer": lambda value: value["acquirer_job"].update(
                {"gpu_count": 1}
            ),
            "writer-open": lambda value: value["handoff"].update(
                {"no_active_writers": False}
            ),
            "same-node": lambda value: value["replacement"].update(
                {"replacement_node": value["replacement"]["original_node"]}
            ),
            "multi-attach": lambda value: value["replacement"].update(
                {"no_multi_attach": False}
            ),
            "runtime-node-substitution": lambda value: value["runtime"].update(
                {"node": value["replacement"]["original_node"]}
            ),
            "writable-runtime": lambda value: value["runtime"].update(
                {"pvc_read_only": False}
            ),
            "claim-not-retained": lambda value: value["scale_to_zero"].update(
                {"claim_retained": False}
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                digest = qualification["placement_receipt_digest"]
                receipt = json.loads(
                    (evidence / "provider-block-pvc" / f"{digest}.json").read_text()
                )
                receipt.pop("receipt_digest")
                mutate(receipt)
                replacement, _ = self.write_receipt(
                    evidence,
                    "provider-block-pvc",
                    receipt,
                    claims={
                        "model_digest": receipt["model_digest"],
                        "artifact_manifest_digest": receipt[
                            "artifact_manifest_digest"
                        ],
                        "artifact_content_digest": receipt[
                            "artifact_content_digest"
                        ],
                        "content_uri": receipt["content_uri"],
                        "acquisition_receipt_digest": receipt[
                            "acquisition_receipt_digest"
                        ],
                        "storage_class_admission_receipt_digest": receipt[
                            "storage_class_admission_receipt_digest"
                        ],
                        "writer_admission_receipt_digest": receipt[
                            "writer_admission_receipt_digest"
                        ],
                        "storage_class_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["storage_class"])
                        ).hexdigest(),
                        "claim_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["claim"])
                        ).hexdigest(),
                        "acquirer_job_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["acquirer_job"])
                        ).hexdigest(),
                        "writer_fence_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["writer_fence"])
                        ).hexdigest(),
                        "handoff_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["handoff"])
                        ).hexdigest(),
                        "replacement_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["replacement"])
                        ).hexdigest(),
                        "runtime_tuple_digest": receipt["runtime"][
                            "runtime_tuple_digest"
                        ],
                        "runtime_node_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["runtime"]["node"])
                        ).hexdigest(),
                        "semantic_receipt_digest": receipt["runtime"][
                            "semantic_receipt_digest"
                        ],
                        "return_to_zero_receipt_digest": receipt[
                            "scale_to_zero"
                        ]["return_to_zero_receipt_digest"],
                    },
                )
                qualification["placement_receipt_digest"] = replacement
                path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

    def test_signed_protected_class_receipt_rejects_delete_and_parameter_drift(self) -> None:
        cases = {
            "default-delete-class": lambda value: value["storage_class"]["spec"].update(
                {"reclaimPolicy": "Delete"}
            ),
            "volume-type-drift": lambda value: value["storage_class"]["spec"][
                "parameters"
            ].update({"type": "NETWORK_SSD_NON_REPLICATED"}),
            "filesystem-drift": lambda value: value["storage_class"]["spec"][
                "parameters"
            ].update({"csi.storage.k8s.io/fstype": "xfs"}),
            "missing-server-uid": lambda value: value["storage_class"][
                "metadata"
            ].pop("uid"),
            "foreign-observer": lambda value: value["observer"].update(
                {"service_account_name": "default"}
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                placement_digest = qualification["placement_receipt_digest"]
                placement = json.loads(
                    (evidence / "provider-block-pvc" / f"{placement_digest}.json").read_text()
                )
                original_digest = placement["storage_class_admission_receipt_digest"]
                receipt = json.loads(
                    (
                        evidence
                        / "protected-storage-classes"
                        / f"{original_digest}.json"
                    ).read_text()
                )
                receipt.pop("receipt_digest")
                mutate(receipt)
                replacement, _ = self.write_receipt(
                    evidence,
                    "protected-storage-classes",
                    receipt,
                    claims={
                        "model_digest": receipt["model_digest"],
                        "storage_class_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["storage_class"])
                        ).hexdigest(),
                        "intended_claim_sha256": hashlib.sha256(
                            canonical_bytes(receipt["intended_claim"])
                        ).hexdigest(),
                        "observer_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["observer"])
                        ).hexdigest(),
                    },
                )
                self.secure_evidence_tree(evidence)
                with self.assertRaises(CatalogError):
                    load_protected_storage_class_admission(
                        catalog.model("qwen3-8b"),
                        evidence,
                        receipt_digest=replacement,
                        evidence_session_id=self.session_id,
                        trusted_attestors=self.trusted_attestors,
                        validation_time=self.validation_time,
                    )

    def test_signed_writer_admission_rejects_same_node_writer_and_fence_substitution(self) -> None:
        cases = {
            "api-fence-not-applied": lambda value: value["api_fence"].update(
                {"api_server_applied": False}
            ),
            "second-writer-not-denied": lambda value: value["api_fence"].update(
                {"second_writer_denied": False}
            ),
            "foreign-writer": lambda value: value["api_fence"].update(
                {"allowed_writer_name": "qwen3-8b-cache-foreign"}
            ),
            "foreign-policy": lambda value: value["controller"].update(
                {"validating_admission_policy_name": "foreign-writer-policy"}
            ),
            "lease-holder-substitution": lambda value: value["lease"].update(
                {"holder_identity": "foreign-operation:qwen3-8b-cache-foreign"}
            ),
            "claim-version-substitution": lambda value: value["api_fence"].update(
                {"claim_resource_version": "999"}
            ),
            "paginated-mount-set": lambda value: value["mount_set"].update(
                {"continue_token": "next-page", "complete": False}
            ),
            "same-node-existing-writer": lambda value: value["mount_set"].update(
                {
                    "mounts": [
                        {
                            "pod_uid": "20202020-2020-2020-2020-202020202020",
                            "node_uid": "21212121-2121-2121-2121-212121212121",
                            "read_only": False,
                        }
                    ]
                }
            ),
            "foreign-job-creator": lambda value: value["api_fence"].update(
                {
                    "allowed_creator_service_account_uid": (
                        "22222222-2222-2222-2222-222222222222"
                    )
                }
            ),
            "writer-create-role-substitution": lambda value: value[
                "api_fence"
            ].update({"writer_create_role_uid": "23232323-2323-2323-2323-232323232323"}),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                record = catalog.model("qwen3-8b")
                placement_digest = qualification["placement_receipt_digest"]
                placement = json.loads(
                    (evidence / "provider-block-pvc" / f"{placement_digest}.json").read_text()
                )
                storage_digest = placement["storage_class_admission_receipt_digest"]
                self.secure_evidence_tree(evidence)
                storage_admission = load_protected_storage_class_admission(
                    record,
                    evidence,
                    receipt_digest=storage_digest,
                    evidence_session_id=self.session_id,
                    trusted_attestors=self.trusted_attestors,
                    validation_time=self.validation_time,
                )
                original_digest = placement["writer_admission_receipt_digest"]
                receipt = json.loads(
                    (
                        evidence
                        / "provider-block-writer-admissions"
                        / f"{original_digest}.json"
                    ).read_text()
                )
                receipt.pop("receipt_digest")
                mutate(receipt)
                replacement, _ = self.write_receipt(
                    evidence,
                    "provider-block-writer-admissions",
                    receipt,
                    claims={
                        "model_digest": receipt["model_digest"],
                        "storage_class_receipt_digest": receipt[
                            "storage_class_receipt_digest"
                        ],
                        "operation_id": receipt["operation_id"],
                        "claim_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["claim"])
                        ).hexdigest(),
                        "writer_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["writer"])
                        ).hexdigest(),
                        "controller_identity_sha256": receipt["controller"][
                            "identity_sha256"
                        ],
                        "lease_identity_sha256": hashlib.sha256(
                            canonical_bytes(receipt["lease"])
                        ).hexdigest(),
                        "complete_mount_set_sha256": hashlib.sha256(
                            canonical_bytes(receipt["mount_set"])
                        ).hexdigest(),
                        "api_fence_sha256": hashlib.sha256(
                            canonical_bytes(receipt["api_fence"])
                        ).hexdigest(),
                    },
                )
                self.secure_evidence_tree(evidence)
                with self.assertRaises(CatalogError):
                    load_provider_block_writer_admission(
                        record,
                        storage_admission,
                        evidence,
                        receipt_digest=replacement,
                        evidence_session_id=self.session_id,
                        trusted_attestors=self.trusted_attestors,
                        validation_time=self.validation_time,
                    )

    def test_base_promotion_gates_fail_independently(self) -> None:
        mutations = {
            "license": (
                lambda value: value["model"]["source"]["license"].update(
                    {"id": "UNVERIFIED", "state": "unverified"}
                ),
                "verified license",
            ),
            "entitlement": (
                lambda value: value["model"]["source"]["entitlement"].update(
                    {
                        "required": True,
                        "state": "unverified",
                        "credential_contract": "platform-managed-token/v1",
                    }
                ),
                "satisfied entitlement",
            ),
            "image": (
                lambda value: value["runtime"]["image"].update(
                    {"reference": None, "state": "historical-redacted"}
                ),
                "immutable image and model",
            ),
            "b300": (
                lambda value: value["resources"]["gpu"].update({"b300_state": "unverified"}),
                "qualified B300 or qualified exact alternative backend",
            ),
            "artifact": (
                lambda value: value["cache"]["artifact"].update(
                    {"state": "historical-verified", "staged": False}
                ),
                "platform-verified immutable artifact manifest",
            ),
            "support": (
                lambda value: value["support"].update({"state": "unqualified"}),
                "unqualified Qwen requires reacquired per-file SHA-256 weights",
            ),
        }
        for name, (mutate, message) in mutations.items():
            with self.subTest(gate=name):
                _, target = self.copy_catalog()
                value = self.promote_qwen(
                    target, hashlib.sha256(b"promotion-gate-manifest").hexdigest()
                )
                mutate(value)
                (target / "models" / "qwen3-8b.json").write_text(json.dumps(value) + "\n")
                self.refresh_scale_contract(target, "qwen3-8b")
                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_semantic_receipt_is_required_by_the_overlay(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        value = self.binding_value(catalog, qualification)
        value["bindings"]["qwen3-8b"]["qualification"]["semantic_evidence_digest"] = None
        path = self.write_binding(target, value)
        with self.assertRaisesRegex(CatalogError, "semantic qualification evidence"):
            self.load_live_bindings(path, catalog, evidence)

    def test_signed_backend_receipt_is_required_by_the_overlay(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        qualification["backend_evidence_digest"] = None
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "signed backend identity evidence"):
            self.load_live_bindings(path, catalog, evidence)

    def test_backend_attestation_wrong_subject_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        digest = qualification["backend_evidence_digest"]
        attestation = evidence / "attestations" / "backends" / f"{digest}.json"
        self.replace_attestation(attestation, model_id="molmim")
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "subject does not match"):
            self.load_live_bindings(path, catalog, evidence)

    def test_acquisition_and_prerequisite_receipts_are_required_by_the_overlay(self) -> None:
        for field, message in (
            ("acquisition_receipt_digest", "artifact acquisition receipt"),
            ("prerequisite_receipt_digest", "prerequisite observation receipt"),
        ):
            with self.subTest(field=field):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                qualification[field] = None
                path = self.write_binding(target, self.binding_value(catalog, qualification))
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_new_promotion_receipts_reject_wrong_subject_and_replayed_nonce(self) -> None:
        for case, message in (
            ("wrong-acquisition-subject", "subject does not match"),
            ("replayed-prerequisite-nonce", "nonce was replayed"),
        ):
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                acquisition_digest = qualification["acquisition_receipt_digest"]
                prerequisite_digest = qualification["prerequisite_receipt_digest"]
                if case == "wrong-acquisition-subject":
                    path = (
                        evidence
                        / "attestations"
                        / "acquisition"
                        / f"{acquisition_digest}.json"
                    )
                    self.replace_attestation(path, model_id="glm-5-2-fp8")
                else:
                    acquisition_attestation = json.loads(
                        (
                            evidence
                            / "attestations"
                            / "acquisition"
                            / f"{acquisition_digest}.json"
                        ).read_text()
                    )
                    path = (
                        evidence
                        / "attestations"
                        / "prerequisites"
                        / f"{prerequisite_digest}.json"
                    )
                    old = json.loads(path.read_text())
                    replacement = create_signed_attestation(
                        private_key=self.attestor,
                        session_id=self.session_id,
                        nonce=acquisition_attestation["nonce"],
                        issued_at="2026-08-26T22:20:00Z",
                        expires_at="2026-08-26T23:00:00Z",
                        kind="prerequisites",
                        subject_schema=old["subject"]["schema"],
                        subject_digest=prerequisite_digest,
                        model_id="qwen3-8b",
                        claims=old["claims"],
                    )
                    path.write_text(json.dumps(replacement) + "\n")
                binding_path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(binding_path, catalog, evidence)

    def test_live_prerequisite_receipt_rejects_legacy_hmac_reuse(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["prerequisite_receipt_digest"]
        value = json.loads((evidence / "prerequisites" / f"{old_digest}.json").read_text())
        value.pop("receipt_digest")
        value["observation"]["legacy_phase_7c_hmac_reused"] = True
        replacement_digest, _ = self.write_receipt(
            evidence,
            "prerequisites",
            value,
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": value["acquisition_plan_sha256"],
                "resource_identity_set_sha256": hashlib.sha256(
                    canonical_bytes(value["observation"]["resources"])
                ).hexdigest(),
                "values_suppressed": True,
                "legacy_ngc_secret_copied": False,
                "legacy_plaintext_rotation_source_used": False,
                "legacy_phase_7c_hmac_reused": True,
                "exposed_evo_bearer_reused": False,
                "ngc_credential_materialization_sha256": None,
            },
        )
        qualification["prerequisite_receipt_digest"] = replacement_digest
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "Phase-7c HMAC reuse is forbidden"):
            self.load_live_bindings(path, catalog, evidence)

    def test_acquisition_receipt_cannot_drift_from_exact_revision(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["acquisition_receipt_digest"]
        value = json.loads((evidence / "acquisition" / f"{old_digest}.json").read_text())
        value.pop("receipt_digest")
        value["source"]["revision"] = "f" * 40
        replacement_digest, _ = self.write_receipt(
            evidence,
            "acquisition",
            value,
            claims={
                "model_digest": catalog.model("qwen3-8b").digest,
                "acquisition_plan_sha256": hashlib.sha256(
                    canonical_bytes(catalog.acquisition_plan("qwen3-8b").to_dict())
                ).hexdigest(),
                "artifact_manifest_digest": value["artifact_manifest_digest"],
                "artifact_content_digest": value["artifact_content_digest"],
                "content_uri": value["content_uri"],
                "prerequisite_set_sha256": hashlib.sha256(
                    canonical_bytes(value["prerequisite_ids"])
                ).hexdigest(),
            },
        )
        qualification["acquisition_receipt_digest"] = replacement_digest
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "source differs from the exact plan"):
            self.load_live_bindings(path, catalog, evidence)

    def test_provider_block_acquisition_rejects_identity_and_ext4_proof_substitution(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["acquisition_receipt_digest"]
        original = json.loads(
            (evidence / "acquisition" / f"{old_digest}.json").read_text()
        )
        cases = {
            "root-uid": lambda value: value["execution"].update({"run_as_uid": 0}),
            "gid-drift": lambda value: value["execution"].update({"run_as_gid": 10002}),
            "fs-group-drift": lambda value: value["execution"].update({"fs_group": 10002}),
            "helper-image-substitution": lambda value: value["helper_image"].update(
                {
                    "reference": "registry.example.invalid/foreign/helper@"
                    + value["helper_image"]["digest"]
                }
            ),
            "job-uid-substitution": lambda value: value["execution"]["job"].update(
                {"uid": "30303030-3030-3030-3030-303030303030"}
            ),
            "pod-owner-shortcut": lambda value: value["execution"]["pod"].update(
                {"owner_job_uid": "30303030-3030-3030-3030-303030303030"}
            ),
            "cleanup-precondition-substitution": lambda value: value["cleanup"][
                "resources"
            ][0].update(
                {"delete_precondition_uid": "30303030-3030-3030-3030-303030303030"}
            ),
            "cleanup-replacement-reuse": lambda value: value["cleanup"]["resources"][
                1
            ].update({"replacement_uid": value["cleanup"]["resources"][1]["uid"]}),
            "cleanup-foreign-uid": lambda value: value["cleanup"].update(
                {"foreign_uids_touched": True}
            ),
            "filesystem-drift": lambda value: value["filesystem_write_proof"].update(
                {"filesystem_type": "xfs"}
            ),
            "probe-path-substitution": lambda value: value[
                "filesystem_write_proof"
            ].update({"probe_path": "/mnt/fs2-provider-block/models/other"}),
            "proof-owner-substitution": lambda value: value[
                "filesystem_write_proof"
            ].update({"file_uid": 10002}),
            "marker-left-behind": lambda value: value[
                "filesystem_write_proof"
            ].update({"marker_removed": False}),
            "directory-not-synced": lambda value: value[
                "filesystem_write_proof"
            ].update({"directory_fsync": False}),
        }
        plan_digest = hashlib.sha256(
            canonical_bytes(catalog.acquisition_plan("qwen3-8b").to_dict())
        ).hexdigest()
        for case, mutate in cases.items():
            with self.subTest(case=case):
                value = copy.deepcopy(original)
                value.pop("receipt_digest")
                mutate(value)
                replacement_digest, _ = self.write_receipt(
                    evidence,
                    "acquisition",
                    value,
                    claims={
                        "model_digest": catalog.model("qwen3-8b").digest,
                        "acquisition_plan_sha256": plan_digest,
                        "artifact_manifest_digest": value[
                            "artifact_manifest_digest"
                        ],
                        "artifact_content_digest": value["artifact_content_digest"],
                        "content_uri": value["content_uri"],
                        "prerequisite_set_sha256": hashlib.sha256(
                            canonical_bytes(value["prerequisite_ids"])
                        ).hexdigest(),
                        "storage_contract_sha256": hashlib.sha256(
                            canonical_bytes(value["storage"])
                        ).hexdigest(),
                        "execution_identity_sha256": hashlib.sha256(
                            canonical_bytes(value["execution"])
                        ).hexdigest(),
                        "worker_result_digest": value["worker_result_digest"],
                        "helper_image_identity_sha256": hashlib.sha256(
                            canonical_bytes(value["helper_image"])
                        ).hexdigest(),
                        "cleanup_identity_sha256": hashlib.sha256(
                            canonical_bytes(value["cleanup"])
                        ).hexdigest(),
                        "filesystem_write_proof_sha256": hashlib.sha256(
                            canonical_bytes(value["filesystem_write_proof"])
                        ).hexdigest(),
                    },
                )
                candidate = copy.deepcopy(qualification)
                candidate["acquisition_receipt_digest"] = replacement_digest
                path = self.write_binding(
                    target, self.binding_value(catalog, candidate)
                )
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

    def test_enabled_binding_rejects_placeholder_digests_without_evidence_root(self) -> None:
        _, target = self.copy_catalog()
        catalog, _, qualification, _ = self.live_fixture(target)
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "immutable evidence root"):
            load_serving_bindings(
                path,
                catalog,
                validation_time=self.validation_time,
            )

    def test_receipt_content_cannot_change_under_the_same_digest(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        digest = qualification["runtime_tuple_digest"]
        path = evidence / "runtime-tuples" / f"{digest}.json"
        value = json.loads(path.read_text())
        value["worker"]["nvidia_driver_version"] = "999.999.99"
        path.write_text(json.dumps(value) + "\n")
        binding_path = self.write_binding(
            target, self.binding_value(catalog, qualification)
        )
        with self.assertRaisesRegex(CatalogError, "receipt content digest failed"):
            self.load_live_bindings(binding_path, catalog, evidence)

    def test_signed_evidence_rejects_forgery_subject_session_freshness_and_claim_drift(self) -> None:
        cases = {
            "forged-signature": "signature verification failed",
            "wrong-subject": "subject does not match",
            "wrong-session": "another evidence session",
            "expired": "not fresh",
            "worker-claim-drift": "signed claims do not match",
        }
        for case, message in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                digest = qualification["runtime_tuple_digest"]
                path = evidence / "attestations" / "runtime-tuples" / f"{digest}.json"
                if case == "forged-signature":
                    value = json.loads(path.read_text())
                    replacement = "A" if value["signature"][0] != "A" else "B"
                    value["signature"] = replacement + value["signature"][1:]
                    path.write_text(json.dumps(value) + "\n")
                elif case == "wrong-subject":
                    self.replace_attestation(path, model_id="glm-5-2-fp8")
                elif case == "wrong-session":
                    self.replace_attestation(
                        path,
                        session_id=hashlib.sha256(b"another-session").hexdigest(),
                    )
                elif case == "expired":
                    self.replace_attestation(
                        path,
                        issued_at="2026-08-26T21:00:00Z",
                        expires_at="2026-08-26T22:10:00Z",
                    )
                else:
                    claims = json.loads(path.read_text())["claims"]
                    claims["worker_image_digest"] = "sha256:" + hashlib.sha256(
                        b"substituted-worker"
                    ).hexdigest()
                    self.replace_attestation(path, claims=claims)
                binding_path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(binding_path, catalog, evidence)

    def test_replayed_semantic_receipt_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        prepared = json.loads(
            (
                evidence
                / "qualifications"
                / f"{qualification['prepared_qualification_digest']}.json"
            ).read_text()
        )
        prepared.pop("receipt_digest")
        prepared["attempts"][1]["semantic_receipt_digest"] = prepared["attempts"][0][
            "semantic_receipt_digest"
        ]
        replacement_digest, _ = self.write_receipt(
            evidence,
            "qualifications",
            prepared,
            claims={
                "cohort": "prepared-node",
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "placement_receipt_digest": qualification["placement_receipt_digest"],
                "attempt_set_sha256": hashlib.sha256(
                    canonical_bytes(prepared["attempts"])
                ).hexdigest(),
            },
        )
        qualification["prepared_qualification_digest"] = replacement_digest
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "distinct semantic receipts|replayed"):
            self.load_live_bindings(path, catalog, evidence)

    def test_cohort_mechanism_must_equal_the_reopened_runtime_tuple(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["prepared_qualification_digest"]
        value = json.loads(
            (evidence / "qualifications" / f"{old_digest}.json").read_text()
        )
        value.pop("receipt_digest")
        value["startup_mechanism"] = "snapshot"
        replacement, _ = self.write_receipt(
            evidence,
            "qualifications",
            value,
            claims={
                "cohort": "prepared-node",
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "placement_receipt_digest": qualification["placement_receipt_digest"],
                "attempt_set_sha256": hashlib.sha256(
                    canonical_bytes(value["attempts"])
                ).hexdigest(),
            },
        )
        qualification["prepared_qualification_digest"] = replacement
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "differs from the exact runtime tuple"):
            self.load_live_bindings(path, catalog, evidence)

    def test_cohort_timestamps_and_claimed_durations_must_reconcile(self) -> None:
        for case, message in (
            ("reversed", "completion precedes"),
            ("duration-drift", "duration differs"),
        ):
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                old_digest = qualification["prepared_qualification_digest"]
                value = json.loads(
                    (evidence / "qualifications" / f"{old_digest}.json").read_text()
                )
                value.pop("receipt_digest")
                if case == "reversed":
                    value["attempts"][0]["completion_utc"] = "2026-08-26T22:00:59Z"
                else:
                    value["attempts"][0]["t0_to_call2_seconds"] = 29.0
                replacement, _ = self.write_receipt(
                    evidence,
                    "qualifications",
                    value,
                    claims={
                        "cohort": "prepared-node",
                        "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                        "placement_receipt_digest": qualification["placement_receipt_digest"],
                        "attempt_set_sha256": hashlib.sha256(
                            canonical_bytes(value["attempts"])
                        ).hexdigest(),
                    },
                )
                qualification["prepared_qualification_digest"] = replacement
                path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_cleanup_receipt_binds_attempt_runtime_node_gpu_and_expected_uids(self) -> None:
        cases = {
            "attempt": (
                lambda value: value.update({"attempt_id": "another-attempt"}),
                "another attempt",
            ),
            "runtime": (
                lambda value: value.update(
                    {"runtime_tuple_digest": hashlib.sha256(b"other-runtime").hexdigest()}
                ),
                "another runtime tuple",
            ),
            "node": (
                lambda value: value["node_identity"].update(
                    {"uid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
                ),
                "another serving Node",
            ),
            "gpu": (
                lambda value: value["gpu_identity"].update(
                    {
                        "allocated_uuids": [
                            "GPU-22222222-2222-2222-2222-222222222222"
                        ]
                    }
                ),
                "another runtime GPU tuple",
            ),
            "expected-uids": (
                lambda value: (
                    value["expected_resource_uids"][0].update(
                        {"uid": "77777777-7777-7777-7777-777777777777"}
                    ),
                    value["resources"][0].update(
                        {
                            "uid": "77777777-7777-7777-7777-777777777777",
                            "precondition_uid": "77777777-7777-7777-7777-777777777777",
                        }
                    ),
                ),
                "expected UID set",
            ),
        }
        for case, (mutate, message) in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                cohort_digest = qualification["prepared_qualification_digest"]
                cohort = json.loads(
                    (evidence / "qualifications" / f"{cohort_digest}.json").read_text()
                )
                cohort.pop("receipt_digest")
                attempt = cohort["attempts"][0]
                cleanup_digest = attempt["cleanup_receipt_digest"]
                cleanup = json.loads(
                    (evidence / "cleanup" / f"{cleanup_digest}.json").read_text()
                )
                cleanup.pop("receipt_digest")
                mutate(cleanup)
                cleanup_claims = {
                    "attempt_id": cleanup["attempt_id"],
                    "runtime_tuple_digest": cleanup["runtime_tuple_digest"],
                    "node_identity_sha256": hashlib.sha256(
                        canonical_bytes(cleanup["node_identity"])
                    ).hexdigest(),
                    "gpu_identity_sha256": hashlib.sha256(
                        canonical_bytes(cleanup["gpu_identity"])
                    ).hexdigest(),
                    "namespace": cleanup["namespace"],
                    "expected_resource_uid_set_sha256": hashlib.sha256(
                        canonical_bytes(cleanup["expected_resource_uids"])
                    ).hexdigest(),
                    "resource_set_sha256": hashlib.sha256(
                        canonical_bytes(cleanup["resources"])
                    ).hexdigest(),
                    "retained_artifact_set_sha256": hashlib.sha256(
                        canonical_bytes(cleanup["retained_artifact_digests"])
                    ).hexdigest(),
                }
                replacement_cleanup, _ = self.write_receipt(
                    evidence, "cleanup", cleanup, claims=cleanup_claims
                )
                attempt["cleanup_receipt_digest"] = replacement_cleanup
                replacement_cohort, _ = self.write_receipt(
                    evidence,
                    "qualifications",
                    cohort,
                    claims={
                        "cohort": "prepared-node",
                        "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                        "placement_receipt_digest": cohort[
                            "placement_receipt_digest"
                        ],
                        "attempt_set_sha256": hashlib.sha256(
                            canonical_bytes(cohort["attempts"])
                        ).hexdigest(),
                    },
                )
                qualification["prepared_qualification_digest"] = replacement_cohort
                path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_binding_validity_is_exact_and_exposed_to_long_lived_consumers(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        value = self.binding_value(catalog, qualification)
        path = self.write_binding(target, value)
        bindings = self.load_live_bindings(path, catalog, evidence)
        binding = bindings.get("qwen3-8b")
        assert binding is not None
        self.assertEqual("2026-08-26T22:20:30Z", binding.valid_until)
        self.assertTrue(binding.valid_at(self.validation_time))
        self.assertFalse(
            binding.valid_at(datetime(2026, 8, 26, 22, 20, 30, tzinfo=timezone.utc))
        )
        self.assertFalse(
            binding.valid_at(datetime(2026, 8, 26, 22, 20, 31, tzinfo=timezone.utc))
        )
        projected = bind_gateway_catalog(catalog, bindings).model("qwen3-8b").to_dict()
        self.assertEqual("2026-08-26T22:20:30Z", projected["serving"]["valid_until"])

        value["bindings"]["qwen3-8b"]["valid_until"] = "2026-08-26T22:20:29Z"
        path = self.write_binding(target, value)
        with self.assertRaisesRegex(CatalogError, "live controller Lease expiry"):
            self.load_live_bindings(path, catalog, evidence)

        value = self.binding_value(catalog, qualification)
        controller = value["bindings"]["qwen3-8b"]["activation"]["controller"]
        controller["leader_lease_renew_time"] = "2026-08-26T22:19:00Z"
        controller_subject = dict(controller)
        controller_subject.pop("identity_sha256")
        controller["identity_sha256"] = hashlib.sha256(
            canonical_bytes(controller_subject)
        ).hexdigest()
        value["bindings"]["qwen3-8b"]["valid_until"] = "2026-08-26T22:19:30Z"
        path = self.write_binding(target, value)
        with self.assertRaisesRegex(CatalogError, "leader Lease is expired"):
            self.load_live_bindings(path, catalog, evidence)

    def test_gateway_semantic_cannot_substitute_transport_or_auth_identity(self) -> None:
        cases = {
            "service-uid": lambda route: route["backend"].update(
                {"service_uid": "99999999-9999-9999-9999-999999999999"}
            ),
            "origin": lambda route: route["backend"].update(
                {"origin": "http://other.fs2-models.svc.cluster.local:8000"}
            ),
            "gateway": lambda route: route["gateway"].update(
                {"identity_sha256": hashlib.sha256(b"pod-local-shortcut").hexdigest()}
            ),
            "auth": lambda route: route["gateway"].update({"auth_class": "none"}),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                old_digest = qualification["semantic_evidence_digest"]
                value = json.loads(
                    (evidence / "semantic" / f"{old_digest}.json").read_text()
                )
                value.pop("receipt_digest")
                mutate(value["gateway_path"])
                replacement, _ = self.write_receipt(
                    evidence,
                    "semantic",
                    value,
                    claims={
                        "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                        "attempt_id": "gateway-smoke",
                        "validator_identity_sha256": hashlib.sha256(
                            canonical_bytes(value["validator"])
                        ).hexdigest(),
                        "call_set_sha256": hashlib.sha256(
                            canonical_bytes(value["responses"])
                        ).hexdigest(),
                        "gateway_path_sha256": hashlib.sha256(
                            canonical_bytes(value["gateway_path"])
                        ).hexdigest(),
                    },
                )
                qualification["semantic_evidence_digest"] = replacement
                path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(
                    CatalogError,
                    "exact trusted gateway route|provider block runtime",
                ):
                    self.load_live_bindings(path, catalog, evidence)

    def test_reattested_unrelated_requests_cannot_bypass_executable_validator_result(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["semantic_evidence_digest"]
        value = json.loads(
            (evidence / "semantic" / f"{old_digest}.json").read_text()
        )
        value.pop("receipt_digest")
        value["responses"][0]["request_sha256"] = hashlib.sha256(
            b"unrelated-request-first"
        ).hexdigest()
        value["responses"][1]["request_sha256"] = hashlib.sha256(
            b"unrelated-request-second"
        ).hexdigest()
        old_validator_digest = value["validator_result_digest"]
        validator_value = json.loads(
            (
                evidence
                / "semantic-validations"
                / f"{old_validator_digest}.json"
            ).read_text()
        )
        validator_value.pop("receipt_digest")
        validator_value["request_sha256"] = [
            item["request_sha256"] for item in value["responses"]
        ]
        validator_replacement, _ = self.write_receipt(
            evidence,
            "semantic-validations",
            validator_value,
            claims={
                "runtime_identity_digest": qualification["runtime_tuple_digest"],
                "attempt_id": "gateway-smoke",
                "request_contract_sha256": value["request_contract_sha256"],
                "request_asset_set_sha256": value["request_asset_set_sha256"],
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(value["validator"])
                ).hexdigest(),
                "request_set_sha256": hashlib.sha256(
                    canonical_bytes(validator_value["request_sha256"])
                ).hexdigest(),
                "response_set_sha256": hashlib.sha256(
                    canonical_bytes(validator_value["response_sha256"])
                ).hexdigest(),
                **_gateway_claims(value["gateway_path"]),
            },
        )
        value["validator_result_digest"] = validator_replacement
        replacement, _ = self.write_receipt(
            evidence,
            "semantic",
            value,
            claims={
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "attempt_id": "gateway-smoke",
                "request_contract_sha256": value["request_contract_sha256"],
                "request_asset_set_sha256": value["request_asset_set_sha256"],
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(value["validator"])
                ).hexdigest(),
                "call_set_sha256": hashlib.sha256(
                    canonical_bytes(value["responses"])
                ).hexdigest(),
                **_gateway_claims(value["gateway_path"]),
                "validator_result_digest": value["validator_result_digest"],
            },
        )
        qualification["semantic_evidence_digest"] = replacement
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(
            CatalogError,
            "canonical request fixtures|provider block runtime",
        ):
            self.load_live_bindings(path, catalog, evidence)

    def test_fully_reattested_pod_local_shortcut_cannot_satisfy_gateway_smoke(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        old_digest = qualification["semantic_evidence_digest"]
        semantic = json.loads(
            (evidence / "semantic" / f"{old_digest}.json").read_text()
        )
        semantic.pop("receipt_digest")
        semantic["gateway_path"]["transport"]["mode"] = "pod-local-direct"

        old_validator_digest = semantic["validator_result_digest"]
        validator = json.loads(
            (
                evidence
                / "semantic-validations"
                / f"{old_validator_digest}.json"
            ).read_text()
        )
        validator.pop("receipt_digest")
        validator["gateway_path_sha256"] = _gateway_claims(
            semantic["gateway_path"]
        )["gateway_path_sha256"]
        validator_digest, _ = self.write_receipt(
            evidence,
            "semantic-validations",
            validator,
            claims={
                "runtime_identity_digest": qualification["runtime_tuple_digest"],
                "attempt_id": "gateway-smoke",
                "request_contract_sha256": semantic["request_contract_sha256"],
                "request_asset_set_sha256": semantic["request_asset_set_sha256"],
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(semantic["validator"])
                ).hexdigest(),
                "request_set_sha256": hashlib.sha256(
                    canonical_bytes(validator["request_sha256"])
                ).hexdigest(),
                "response_set_sha256": hashlib.sha256(
                    canonical_bytes(validator["response_sha256"])
                ).hexdigest(),
                **_gateway_claims(semantic["gateway_path"]),
            },
        )
        semantic["validator_result_digest"] = validator_digest
        replacement, _ = self.write_receipt(
            evidence,
            "semantic",
            semantic,
            claims={
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "attempt_id": "gateway-smoke",
                "request_contract_sha256": semantic["request_contract_sha256"],
                "request_asset_set_sha256": semantic["request_asset_set_sha256"],
                "validator_identity_sha256": hashlib.sha256(
                    canonical_bytes(semantic["validator"])
                ).hexdigest(),
                "call_set_sha256": hashlib.sha256(
                    canonical_bytes(semantic["responses"])
                ).hexdigest(),
                **_gateway_claims(semantic["gateway_path"]),
                "validator_result_digest": validator_digest,
            },
        )
        qualification["semantic_evidence_digest"] = replacement
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(
            CatalogError,
            "exact trusted gateway route|provider block runtime",
        ):
            self.load_live_bindings(path, catalog, evidence)

    def test_signed_gateway_claims_expose_every_private_route_identity_join(self) -> None:
        _, target = self.copy_catalog()
        _, evidence, qualification, _ = self.live_fixture(target)
        semantic_digest = qualification["semantic_evidence_digest"]
        semantic = json.loads(
            (evidence / "semantic" / f"{semantic_digest}.json").read_text()
        )
        claims = json.loads(
            (
                evidence
                / "attestations"
                / "semantic"
                / f"{semantic_digest}.json"
            ).read_text()
        )["claims"]
        gateway_path = semantic["gateway_path"]
        expected = _gateway_claims(gateway_path)
        self.assertEqual(expected["gateway_path_sha256"], claims["gateway_path_sha256"])
        self.assertEqual("chat", claims["operation"])
        self.assertEqual(
            gateway_path["gateway"]["service_uid"], claims["gateway_service_uid"]
        )
        self.assertEqual(
            gateway_path["backend"]["service_uid"], claims["backend_service_uid"]
        )
        self.assertEqual(
            expected["transport_identity_sha256"], claims["transport_identity_sha256"]
        )
        self.assertEqual(
            gateway_path["readiness"]["identity_sha256"],
            claims["readiness_identity_sha256"],
        )
        validator_digest = semantic["validator_result_digest"]
        validator = json.loads(
            (
                evidence
                / "semantic-validations"
                / f"{validator_digest}.json"
            ).read_text()
        )
        self.assertEqual(claims["gateway_path_sha256"], validator["gateway_path_sha256"])

    def test_mcp_description_cannot_project_internal_route_material(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        for description in (
            "Call http://qwen3-8b.fs2-models.svc.cluster.local:8000.",
            "Use /internal/activate/qwen3-8b after admission.",
            "The service_origin is cluster private.",
        ):
            with self.subTest(description=description), tempfile.TemporaryDirectory() as temporary:
                value = self.binding_value(catalog, enabled=False)
                value["bindings"]["qwen3-8b"]["mcp"]["description"] = description
                path = self.write_binding(Path(temporary), value)
                with self.assertRaisesRegex(CatalogError, "private route or activation"):
                    load_serving_bindings(path, catalog)

    def test_nimcache_has_a_distinct_signed_owner_readiness_and_sfs_path(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        record = catalog.model("molmim")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "cache.bin").write_bytes(b"unit-test-nim-cache")
            manifest = build_artifact_manifest(
                source,
                model_id="molmim",
                kind="nim-cache",
                source_uri="ngc://nvcr.io/nim/nvidia/molmim",
                source_revision=record.to_dict()["model"]["source"]["revision"],
                license_id="NVIDIA-AI-Enterprise",
                license_state="verified",
                entitlement_state="verified",
                owner="nim-operator-nimcache",
                retention="retained-platform",
            )
            evidence = root / "evidence"
            evidence.mkdir()
            content_uri = (
                "sfs://fs2-cache/mnt/fs2-serve-cache/models/molmim/sha256/"
                + manifest.content_digest
            )
            cache = {
                "api_version": "apps.nvidia.com/v1alpha1",
                "kind": "NIMCache",
                "namespace": "fs2-models",
                "name": "molmim",
                "uid": "55555555-5555-5555-5555-555555555555",
                "resource_version": "21",
                "observed_generation": 3,
                "cache_state": "Ready",
            }
            pvc = {
                "namespace": "fs2-models",
                "name": "molmim-cache",
                "uid": "66666666-6666-6666-6666-666666666666",
                "resource_version": "22",
                "state": "Bound",
            }
            prerequisite_digest = hashlib.sha256(
                b"unit-fresh-ngc-prerequisite-receipt"
            ).hexdigest()
            credential_materialization = make_ngc_materialization(
                {
                    "fs2-models/ngc-pull-secret": {
                        "namespace": "fs2-models",
                        "name": "fs2-ngc-pull",
                        "uid": "77777777-7777-7777-7777-777777777777",
                        "resource_version": "31",
                    },
                    "fs2-models/ngc-runtime-secret": {
                        "namespace": "fs2-models",
                        "name": "fs2-ngc-runtime",
                        "uid": "88888888-8888-8888-8888-888888888888",
                        "resource_version": "32",
                    },
                },
                materialized_at="2026-08-26T22:15:00Z",
            )
            materialization_digest = hashlib.sha256(
                canonical_bytes(credential_materialization)
            ).hexdigest()
            prerequisite_subject = {
                "observation": {
                    "ngc_credential_materialization": credential_materialization,
                }
            }
            credential_authentication = {
                "status": "PASS",
                "prerequisite_receipt_digest": prerequisite_digest,
                "credential_materialization_sha256": materialization_digest,
                "secret_requirement_ids": [
                    "fs2-models/ngc-pull-secret",
                    "fs2-models/ngc-runtime-secret",
                ],
                "secret_resource_uids": [
                    {
                        "requirement_id": "fs2-models/ngc-pull-secret",
                        "uid": "77777777-7777-7777-7777-777777777777",
                    },
                    {
                        "requirement_id": "fs2-models/ngc-runtime-secret",
                        "uid": "88888888-8888-8888-8888-888888888888",
                    },
                ],
                "values_suppressed": True,
            }
            digest, receipt = self.write_receipt(
                evidence,
                "nim-cache",
                {
                    "schema": "fs2-serve.nebius.ai/nim-cache-readiness-receipt/v2",
                    "status": "Ready",
                    "checked_at": "2026-08-26T22:18:00Z",
                    "model_id": "molmim",
                    "model_digest": record.digest,
                    "artifact_manifest_digest": manifest.digest,
                    "content_digest": manifest.content_digest,
                    "content_uri": content_uri,
                    "controller_owner": "nim-operator-nimcache",
                    "nim_cache": cache,
                    "persistent_volume_claim": pvc,
                    "runtime_image_digest": record.to_dict()["runtime"]["image"]["digest"],
                    "credential_authentication": credential_authentication,
                },
                claims={
                    "model_digest": record.digest,
                    "artifact_manifest_digest": manifest.digest,
                    "artifact_content_digest": manifest.content_digest,
                    "content_uri": content_uri,
                    "runtime_image_digest": record.to_dict()["runtime"]["image"]["digest"],
                    "nim_cache_identity_sha256": hashlib.sha256(
                        canonical_bytes(cache)
                    ).hexdigest(),
                    "pvc_identity_sha256": hashlib.sha256(
                        canonical_bytes(pvc)
                    ).hexdigest(),
                    "credential_authentication_sha256": hashlib.sha256(
                        canonical_bytes(credential_authentication)
                    ).hexdigest(),
                },
            )
            self.secure_evidence_tree(evidence)
            store = EvidenceStore(
                evidence,
                session_id=self.session_id,
                trusted_attestors=self.trusted_attestors,
                validation_time=self.validation_time,
            )
            self.assertEqual(
                "Ready",
                _validate_nim_cache_readiness(
                    store,
                    digest,
                    record,
                    manifest,
                    content_uri,
                    prerequisite_subject=prerequisite_subject,
                    prerequisite_digest=prerequisite_digest,
                )["status"],
            )
            self.assertEqual("nim-operator-nimcache", receipt["controller_owner"])
            adversary = dict(receipt)
            adversary.pop("receipt_digest")
            adversary["controller_owner"] = "fs2-serve-localizer"
            bad_digest, _ = self.write_receipt(
                evidence,
                "nim-cache",
                adversary,
                claims={
                    "model_digest": record.digest,
                    "artifact_manifest_digest": manifest.digest,
                    "artifact_content_digest": manifest.content_digest,
                    "content_uri": content_uri,
                    "runtime_image_digest": record.to_dict()["runtime"]["image"]["digest"],
                    "nim_cache_identity_sha256": hashlib.sha256(
                        canonical_bytes(cache)
                    ).hexdigest(),
                    "pvc_identity_sha256": hashlib.sha256(
                        canonical_bytes(pvc)
                    ).hexdigest(),
                    "credential_authentication_sha256": hashlib.sha256(
                        canonical_bytes(credential_authentication)
                    ).hexdigest(),
                },
            )
            self.secure_evidence_tree(evidence)
            bad_store = EvidenceStore(
                evidence,
                session_id=self.session_id,
                trusted_attestors=self.trusted_attestors,
                validation_time=self.validation_time,
            )
            with self.assertRaisesRegex(CatalogError, "foreign controller owner"):
                _validate_nim_cache_readiness(
                    bad_store,
                    bad_digest,
                    record,
                    manifest,
                    content_uri,
                    prerequisite_subject=prerequisite_subject,
                    prerequisite_digest=prerequisite_digest,
                )

            substituted = dict(receipt)
            substituted.pop("receipt_digest")
            substituted["credential_authentication"] = dict(
                substituted["credential_authentication"]
            )
            substituted["credential_authentication"][
                "credential_materialization_sha256"
            ] = hashlib.sha256(b"substituted-ngc-generation").hexdigest()
            substituted_digest, _ = self.write_receipt(
                evidence,
                "nim-cache",
                substituted,
                claims={
                    "model_digest": record.digest,
                    "artifact_manifest_digest": manifest.digest,
                    "artifact_content_digest": manifest.content_digest,
                    "content_uri": content_uri,
                    "runtime_image_digest": record.to_dict()["runtime"]["image"][
                        "digest"
                    ],
                    "nim_cache_identity_sha256": hashlib.sha256(
                        canonical_bytes(cache)
                    ).hexdigest(),
                    "pvc_identity_sha256": hashlib.sha256(
                        canonical_bytes(pvc)
                    ).hexdigest(),
                    "credential_authentication_sha256": hashlib.sha256(
                        canonical_bytes(substituted["credential_authentication"])
                    ).hexdigest(),
                },
            )
            self.secure_evidence_tree(evidence)
            substituted_store = EvidenceStore(
                evidence,
                session_id=self.session_id,
                trusted_attestors=self.trusted_attestors,
                validation_time=self.validation_time,
            )
            with self.assertRaisesRegex(
                CatalogError, "differs from fresh credential evidence"
            ):
                _validate_nim_cache_readiness(
                    substituted_store,
                    substituted_digest,
                    record,
                    manifest,
                    content_uri,
                    prerequisite_subject=prerequisite_subject,
                    prerequisite_digest=prerequisite_digest,
                )

    def test_faststart_job_requires_reopened_signed_tuple_and_exact_job_subject(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        record = catalog.model("qwen3-8b")
        image = "registry.example.invalid/fs2/faststart@sha256:" + hashlib.sha256(
            b"faststart-job-image"
        ).hexdigest()
        command = ["/usr/local/bin/fs2-faststart", "donor", "--model", "qwen3-8b"]
        command_sha256 = hashlib.sha256(canonical_bytes(command)).hexdigest()
        admission_digest, _ = self.write_receipt(
            evidence,
            "faststart-admissions",
            {
                "schema": "fs2-serve.nebius.ai/faststart-job-admission/v2",
                "status": "approved",
                "approved_at": "2026-08-26T22:20:00Z",
                "model_id": "qwen3-8b",
                "model_digest": record.digest,
                "job_kind": "donor",
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "artifact_manifest_digest": qualification["artifact_manifest_digest"],
                "placement_receipt_digest": qualification["placement_receipt_digest"],
                "prerequisite_receipt_digest": None,
                "content_uri": qualification["artifact_uri"],
                "job": {
                    "image": image,
                    "command": command,
                    "command_sha256": command_sha256,
                },
                "review_scope": "reviewed-single-b300-faststart/v1",
            },
            claims={
                "model_digest": record.digest,
                "job_kind": "donor",
                "runtime_tuple_digest": qualification["runtime_tuple_digest"],
                "artifact_manifest_digest": qualification["artifact_manifest_digest"],
                "placement_receipt_digest": qualification["placement_receipt_digest"],
                "prerequisite_receipt_digest": None,
                "content_uri": qualification["artifact_uri"],
                "job_image_digest": image.rsplit("@", 1)[1],
                "command_sha256": command_sha256,
            },
        )
        self.secure_evidence_tree(evidence)
        with self.assertRaisesRegex(CatalogError, "reviewed local-PV/PVC lifecycle"):
            load_faststart_job_admission(
                record,
                evidence,
                admission_digest=admission_digest,
                artifact_manifest_digest=qualification["artifact_manifest_digest"],
                placement_receipt_digest=qualification["placement_receipt_digest"],
                runtime_tuple_digest=qualification["runtime_tuple_digest"],
                content_uri=qualification["artifact_uri"],
                evidence_session_id=self.session_id,
                trusted_attestors=self.trusted_attestors,
                validation_time=self.validation_time,
            )
        with self.assertRaisesRegex(
            CatalogError, "requires the fresh NGC prerequisite receipt"
        ):
            load_faststart_job_admission(
                catalog.model("openfold2"),
                evidence,
                admission_digest=admission_digest,
                artifact_manifest_digest=qualification["artifact_manifest_digest"],
                placement_receipt_digest=qualification["placement_receipt_digest"],
                runtime_tuple_digest=qualification["runtime_tuple_digest"],
                content_uri=qualification["artifact_uri"],
                evidence_session_id=self.session_id,
                trusted_attestors=self.trusted_attestors,
                validation_time=self.validation_time,
            )

    def test_untrusted_attestor_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        digest = qualification["runtime_tuple_digest"]
        path = evidence / "attestations" / "runtime-tuples" / f"{digest}.json"
        self.replace_attestation(path, private_key=Ed25519PrivateKey.generate())
        binding_path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "untrusted key"):
            self.load_live_bindings(binding_path, catalog, evidence)

    def test_backend_identity_and_internal_urls_cannot_be_substituted_or_projected(self) -> None:
        mutations = {
            "namespace": lambda service: service.update({"namespace": "fs2-system"}),
            "name": lambda service: service.update({"name": "attacker"}),
            "port": lambda service: service.update({"port": 9000}),
            "origin": lambda service: service.update(
                {"origin": "http://attacker.fs2-models.svc.cluster.local:8000"}
            ),
            "double-slash": lambda service: service["endpoints"].update(
                {"openai-chat": "/v1//chat/completions"}
            ),
            "traversal": lambda service: service["endpoints"].update(
                {"openai-chat": "/v1/../chat/completions"}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                value = self.binding_value(catalog, qualification)
                mutate(value["bindings"]["qwen3-8b"]["service"])
                path = self.write_binding(target, value)
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

        backend_mutations = {
            "class": lambda backend: backend.update({"class": "federated-serverless"}),
            "region": lambda backend: backend.update({"region": "us-central1"}),
            "gpu": lambda backend: backend.update({"gpu_class": "NVIDIA-H200-SXM"}),
            "runtime-digest": lambda backend: backend.update(
                {"runtime_image_digest": "sha256:" + hashlib.sha256(b"other-image").hexdigest()}
            ),
            "endpoint": lambda backend: backend.update(
                {"endpoint_identity_sha256": hashlib.sha256(b"other-endpoint").hexdigest()}
            ),
            "trust": lambda backend: backend.update(
                {"trust_bundle_sha256": hashlib.sha256(b"other-trust").hexdigest()}
            ),
        }
        for name, mutate in backend_mutations.items():
            with self.subTest(backend=name):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                value = self.binding_value(catalog, qualification)
                mutate(value["bindings"]["qwen3-8b"]["backend"])
                path = self.write_binding(target, value)
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        gateway = bind_gateway_catalog(catalog, self.load_live_bindings(path, catalog, evidence))
        serving = gateway.model("qwen3-8b").to_dict()["serving"]
        self.assertNotIn("service_origin", serving)
        self.assertNotIn("activation_url", serving)
        self.assertNotIn("activation", serving)
        self.assertEqual("local-kubernetes", serving["backend"]["class"])
        self.assertEqual("us-north1", serving["backend"]["region"])
        self.assertNotIn("endpoint_identity_sha256", serving["backend"])
        self.assertNotIn("trust_bundle_sha256", serving["backend"])
        self.assertNotIn("origin", serving["backend"])

    def test_activation_binding_and_lifecycle_receipts_reject_substitution(self) -> None:
        binding_mutations = {
            "scale-contract": lambda item: item.update(
                {"scale_contract_digest": hashlib.sha256(b"other-scale-contract").hexdigest()}
            ),
            "controller-intent-interface": lambda item: item["controller"].update(
                {"intent_interface_sha256": hashlib.sha256(b"other-intent-interface").hexdigest()}
            ),
            "invented-controller-endpoint": lambda item: item["controller"].update(
                {"endpoint": "https://nonexistent.invalid/v1/activate/qwen3-8b"}
            ),
            "controller-deployment-uid": lambda item: item["controller"].update(
                {"deployment_uid": "99999999-9999-9999-9999-999999999999"}
            ),
            "controller-leader-role": lambda item: item["controller"].update(
                {"leader_role_name": "foreign-activation-leader"}
            ),
            "controller-target-role": lambda item: item["controller"].update(
                {"target_role_name": "foreign-activation-targets"}
            ),
            "controller-database-grants": lambda item: item["controller"].update(
                {"database_grants_sha256": hashlib.sha256(b"foreign-grants").hexdigest()}
            ),
            "controller-submitter-role": lambda item: item["controller"].update(
                {"submitter_database_role": "fs2_activation_owner"}
            ),
            "controller-claim-owner-role": lambda item: item["controller"].update(
                {"claim_owner_database_role": "fs2_activation_submitter"}
            ),
            "controller-lease-holder": lambda item: item["controller"].update(
                {"leader_lease_holder_identity": "foreign-controller"}
            ),
            "target-uid": lambda item: item["target"].update(
                {"uid": "99999999-9999-9999-9999-999999999999"}
            ),
            "missing-zero-receipt": lambda item: item.update(
                {"zero_to_ready_receipt_digest": None}
            ),
        }
        for case, mutate in binding_mutations.items():
            with self.subTest(binding=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                value = self.binding_value(catalog, qualification)
                mutate(value["bindings"]["qwen3-8b"]["activation"])
                path = self.write_binding(target, value)
                with self.assertRaises(CatalogError):
                    self.load_live_bindings(path, catalog, evidence)

        receipt_mutations = {
            "zero-target": (
                "zero-to-ready",
                lambda value: value["target"].update(
                    {"uid": "99999999-9999-9999-9999-999999999999"}
                ),
                "live activation binding",
            ),
            "zero-duration": (
                "zero-to-ready",
                lambda value: value["timestamps"].update(
                    {"duration_seconds": 1.0}
                ),
                "duration differs",
            ),
            "zero-binding-digest": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {"binding_digest": hashlib.sha256(b"other-binding").hexdigest()}
                ),
                "canonical route subject",
            ),
            "zero-model-substitution": (
                "zero-to-ready",
                lambda value: value["intent"].update({"model_id": "glm-5-2-fp8"}),
                "canonical route subject",
            ),
            "zero-fence-operation-substitution": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {"fence_operation_id": "33333333-3333-4333-8333-333333333333"}
                ),
                "fence operation",
            ),
            "zero-submitter-substitution": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {
                        "submitter_service_account_uid": (
                            "99999999-9999-9999-9999-999999999999"
                        )
                    }
                ),
                "principal or Lease",
            ),
            "zero-claim-owner-substitution": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {
                        "claim_owner_service_account_uid": (
                            "99999999-9999-9999-9999-999999999999"
                        )
                    }
                ),
                "principal or Lease",
            ),
            "zero-leader-lease-substitution": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {"leader_lease_resource_version": "999"}
                ),
                "principal or Lease",
            ),
            "zero-expired-claim": (
                "zero-to-ready",
                lambda value: value["intent"].update(
                    {"claim_lease_expires_at": "2026-08-26T22:11:30Z"}
                ),
                "timestamps are not ordered",
            ),
            "zero-remote-model-argv": (
                "zero-to-ready",
                lambda value: value["runtime_startup"].update(
                    {
                        "effective_argv": [
                            "python3",
                            "-m",
                            "vllm.entrypoints.openai.api_server",
                            "--model",
                            "Qwen/Qwen3-8B",
                        ]
                    }
                ),
                "exact mounted content address",
            ),
            "zero-network-policy-not-observed-before-process": (
                "zero-to-ready",
                lambda value: value["runtime_startup"]["timestamps"].update(
                    {"policy_observed_at": "2026-08-26T22:11:20Z"}
                ),
                "deny-egress before process start",
            ),
            "zero-gitops-owns-replicas": (
                "zero-to-ready",
                lambda value: value["replica_ownership"].update(
                    {"gitops_owns_replicas": True}
                ),
                "activation-only managedFields",
            ),
            "return-operation": (
                "return-to-zero",
                lambda value: value["intent"].update(
                    {"operation_id": "22222222-2222-4222-8222-222222222222"}
                ),
                "operation-free|provider block claim",
            ),
            "return-replays-activation-intent": (
                "return-to-zero",
                lambda value: value["intent"].update(
                    {"intent_id": "11111111-1111-4111-8111-111111111111"}
                ),
                "distinct durable intents|provider block claim",
            ),
            "return-regresses-fencing-token-7-after-19": (
                "return-to-zero",
                lambda value: value["intent"].update(
                    {"previous_fencing_token": 6, "fencing_token": 7}
                ),
                "advance monotonically per model|provider block claim",
            ),
            "return-gitops-owns-replicas": (
                "return-to-zero",
                lambda value: value["replica_ownership"].update(
                    {"foreign_replica_managers": ["argocd-application-controller"]}
                ),
                "activation-only managedFields|provider block claim",
            ),
            "return-foreign-uid": (
                "return-to-zero",
                lambda value: value["cleanup"].update(
                    {"foreign_uids_touched": True}
                ),
                "fenced reclamation|provider block claim",
            ),
        }
        for case, (kind, mutate, message) in receipt_mutations.items():
            with self.subTest(receipt=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, _ = self.live_fixture(target)
                qualification_key = (
                    "activation_zero_to_ready_receipt_digest"
                    if kind == "zero-to-ready"
                    else "activation_return_to_zero_receipt_digest"
                )
                digest = qualification[qualification_key]
                receipt = json.loads(
                    (evidence / kind / f"{digest}.json").read_text()
                )
                receipt.pop("receipt_digest")
                mutate(receipt)
                controller = receipt["controller"]
                target_subject = receipt["target"]
                intent = receipt["intent"]
                claims = {
                    "model_digest": receipt["model_digest"],
                    "scale_contract_digest": receipt["scale_contract_digest"],
                    "runtime_tuple_digest": receipt["runtime_tuple_digest"],
                    "activation_intent_sha256": hashlib.sha256(
                        canonical_bytes(intent)
                    ).hexdigest(),
                    "operation_id": intent["operation_id"],
                    "operation_attempt": intent["operation_attempt"],
                    "fence_operation_id": intent["fence_operation_id"],
                    "intent_model_id": intent["model_id"],
                    "binding_digest": intent["binding_digest"],
                    "controller_id": intent["controller_id"],
                    "fencing_token": intent["fencing_token"],
                    "submitter_service_account_uid": intent[
                        "submitter_service_account_uid"
                    ],
                    "claim_owner_service_account_uid": intent[
                        "claim_owner_service_account_uid"
                    ],
                    "leader_lease_uid": intent["leader_lease_uid"],
                    "leader_lease_resource_version": intent[
                        "leader_lease_resource_version"
                    ],
                    "leader_lease_holder_identity": intent[
                        "leader_lease_holder_identity"
                    ],
                    "claim_lease_expires_at": intent["claim_lease_expires_at"],
                    "controller_identity_sha256": controller["identity_sha256"],
                    "target_identity_sha256": hashlib.sha256(
                        canonical_bytes(target_subject)
                    ).hexdigest(),
                    "replica_transition_sha256": hashlib.sha256(
                        canonical_bytes(receipt["replicas"])
                    ).hexdigest(),
                    "lifecycle_timestamps_sha256": hashlib.sha256(
                        canonical_bytes(receipt["timestamps"])
                    ).hexdigest(),
                }
                if kind == "zero-to-ready":
                    claims["readiness_observation_sha256"] = hashlib.sha256(
                        canonical_bytes(receipt["readiness"])
                    ).hexdigest()
                else:
                    cleanup = receipt["cleanup"]
                    claims.update(
                        {
                            "drain_sha256": hashlib.sha256(
                                canonical_bytes(receipt["drain"])
                            ).hexdigest(),
                            "expected_resource_uid_set_sha256": hashlib.sha256(
                                canonical_bytes(cleanup["expected_resource_uids"])
                            ).hexdigest(),
                            "resource_result_set_sha256": hashlib.sha256(
                                canonical_bytes(cleanup["resources"])
                            ).hexdigest(),
                            "retained_artifact_set_sha256": hashlib.sha256(
                                canonical_bytes(cleanup["retained_artifact_digests"])
                            ).hexdigest(),
                        }
                    )
                replacement, _ = self.write_receipt(
                    evidence, kind, receipt, claims=claims
                )
                qualification[qualification_key] = replacement
                path = self.write_binding(
                    target, self.binding_value(catalog, qualification)
                )
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_return_to_zero_cannot_replay_the_pre_mutation_target_version(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        zero_digest = qualification["activation_zero_to_ready_receipt_digest"]
        zero = json.loads(
            (evidence / "zero-to-ready" / f"{zero_digest}.json").read_text()
        )
        return_digest = qualification["activation_return_to_zero_receipt_digest"]
        receipt = json.loads(
            (evidence / "return-to-zero" / f"{return_digest}.json").read_text()
        )
        receipt.pop("receipt_digest")
        receipt["target"] = zero["target"]
        claims = {
            "model_digest": receipt["model_digest"],
            "scale_contract_digest": receipt["scale_contract_digest"],
            "runtime_tuple_digest": receipt["runtime_tuple_digest"],
            "activation_intent_sha256": hashlib.sha256(
                canonical_bytes(receipt["intent"])
            ).hexdigest(),
            "operation_id": receipt["intent"]["operation_id"],
            "operation_attempt": receipt["intent"]["operation_attempt"],
            "intent_model_id": receipt["intent"]["model_id"],
            "binding_digest": receipt["intent"]["binding_digest"],
            "controller_id": receipt["intent"]["controller_id"],
            "fencing_token": receipt["intent"]["fencing_token"],
            "claim_lease_expires_at": receipt["intent"]["claim_lease_expires_at"],
            "controller_identity_sha256": receipt["controller"]["identity_sha256"],
            "target_identity_sha256": hashlib.sha256(
                canonical_bytes(receipt["target"])
            ).hexdigest(),
            "replica_transition_sha256": hashlib.sha256(
                canonical_bytes(receipt["replicas"])
            ).hexdigest(),
            "lifecycle_timestamps_sha256": hashlib.sha256(
                canonical_bytes(receipt["timestamps"])
            ).hexdigest(),
            "drain_sha256": hashlib.sha256(
                canonical_bytes(receipt["drain"])
            ).hexdigest(),
            "expected_resource_uid_set_sha256": hashlib.sha256(
                canonical_bytes(receipt["cleanup"]["expected_resource_uids"])
            ).hexdigest(),
            "resource_result_set_sha256": hashlib.sha256(
                canonical_bytes(receipt["cleanup"]["resources"])
            ).hexdigest(),
            "retained_artifact_set_sha256": hashlib.sha256(
                canonical_bytes(receipt["cleanup"]["retained_artifact_digests"])
            ).hexdigest(),
        }
        replacement, _ = self.write_receipt(
            evidence, "return-to-zero", receipt, claims=claims
        )
        qualification["activation_return_to_zero_receipt_digest"] = replacement
        value = self.binding_value(catalog, qualification)
        value["bindings"]["qwen3-8b"]["activation"]["target"] = zero["target"]
        path = self.write_binding(target, value)
        with self.assertRaisesRegex(
            CatalogError, "version did not advance|provider block claim"
        ):
            self.load_live_bindings(path, catalog, evidence)

    def test_disabled_binding_cannot_enable_mcp(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        value = self.binding_value(catalog, enabled=False)
        value["bindings"]["qwen3-8b"]["mcp"]["enabled"] = True
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_binding(Path(temporary), value)
            with self.assertRaisesRegex(CatalogError, "enabled routable base-invocable"):
                load_serving_bindings(path, catalog)

    def test_disabled_molmim_overlay_preserves_exact_federation_without_origin_projection(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        record = catalog.model("molmim")
        inventory = catalog.federated_backend("molmim").to_dict()
        empty = {
            "storage_mode": None,
            "artifact_manifest_digest": None,
            "artifact_uri": None,
            "acquisition_receipt_digest": None,
            "prerequisite_receipt_digest": None,
            "target_node_canary_digest": None,
            "placement_receipt_digest": None,
            "runtime_tuple_digest": None,
            "prepared_qualification_digest": None,
            "new_node_qualification_digest": None,
            "semantic_evidence_digest": None,
            "readiness_evidence_digest": None,
            "backend_evidence_digest": None,
            "federated_qualification_digest": None,
            "evidence_session_id": None,
        }
        value = {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {
                "molmim": {
                    "model_digest": record.digest,
                    "enabled": False,
                    "ready": False,
                    "valid_until": None,
                    "service": {
                        "execution_mode": "http",
                        "namespace": "fs2-models",
                        "name": "molmim",
                        "port": 8000,
                        "origin": "http://molmim.fs2-models.svc.cluster.local:8000",
                        "protocols": ["native"],
                        "endpoints": {"native": "/generate"},
                    },
                    "backend": {
                        "class": inventory["backend_class"],
                        "inventory_model_id": "molmim",
                        "region": inventory["region"],
                        "gpu_class": inventory["gpu_class"],
                        "runtime_image_digest": inventory["runtime_image_digest"],
                        "endpoint_identity_sha256": inventory["endpoint_identity_sha256"],
                        "trust_bundle_sha256": inventory["trust_bundle_sha256"],
                        "credential_requirement_id": inventory["credential_requirement_id"],
                    },
                    "gateway": {
                        "class": "fs2-serve-gateway",
                        "namespace": "fs2-system",
                        "service_name": "fs2-serve-control-plane",
                        "service_uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "port": 8080,
                        "identity_sha256": hashlib.sha256(
                            canonical_bytes(
                                {
                                    "class": "fs2-serve-gateway",
                                    "namespace": "fs2-system",
                                    "service_name": "fs2-serve-control-plane",
                                    "service_uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                                    "port": 8080,
                                }
                            )
                        ).hexdigest(),
                        "auth_class": "scoped-api-key",
                        "route_id": "molmim",
                    },
                    "activation": self.activation_value(
                        catalog, "molmim", enabled=False
                    ),
                    "policy": {"operations": ["generate-molecule"]},
                    "mcp": {
                        "enabled": False,
                        "tool_name": "molmim",
                        "description": "Gated exact H200 upstream.",
                    },
                    "qualification": empty,
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_binding(Path(temporary), value)
            gateway = bind_gateway_catalog(catalog, load_serving_bindings(path, catalog))
        public = gateway.model("molmim").to_dict()
        self.assertFalse(public["routable"])
        self.assertEqual("federated-kserve-nim", public["serving"]["backend"]["class"])
        self.assertEqual("us-central1", public["serving"]["backend"]["region"])
        self.assertEqual(inventory["runtime_image_digest"], public["serving"]["backend"]["runtime_image_digest"])
        self.assertNotIn("origin", public["serving"])
        self.assertNotIn("credential_requirement_id", public["serving"]["backend"])

    def test_exact_federated_route_requires_signed_artifact_readiness_and_semantics(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, binding = self.federated_molmim_fixture(target)
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for federation validation: {exc}")
        Draft202012Validator(
            json.loads(
                (CATALOG_ROOT / "schema" / "serving-bindings.schema.json").read_text()
            )
        ).validate(binding)
        receipt = json.loads(
            (
                evidence
                / "federated-qualifications"
                / f"{qualification['federated_qualification_digest']}.json"
            ).read_text()
        )
        Draft202012Validator(
            json.loads(
                (
                    CATALOG_ROOT
                    / "schema"
                    / "federated-qualification-receipt.schema.json"
                ).read_text()
            )
        ).validate(receipt)
        path = self.write_binding(target, binding)
        gateway = bind_gateway_catalog(
            catalog, self.load_live_bindings(path, catalog, evidence)
        )
        self.assertEqual(("molmim",), gateway.routable_model_ids())
        public = gateway.model("molmim").to_dict()
        self.assertTrue(public["routable"])
        self.assertTrue(public["mcp"]["invocable"])
        self.assertEqual("federated-kserve-nim", public["serving"]["backend"]["class"])
        self.assertEqual("us-central1", public["serving"]["backend"]["region"])
        self.assertNotIn("service_origin", public["serving"])
        self.assertNotIn("activation_url", public["serving"])
        self.assertNotIn("endpoint_identity_sha256", public["serving"]["backend"])
        self.assertNotIn("trust_bundle_sha256", public["serving"]["backend"])
        self.assertNotIn("credential_requirement_id", public["serving"]["backend"])

    def test_local_and_federated_receipt_modes_cannot_be_mixed(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        qualification["federated_qualification_digest"] = hashlib.sha256(
            b"foreign-federated-qualification"
        ).hexdigest()
        path = self.write_binding(target, self.binding_value(catalog, qualification))
        with self.assertRaisesRegex(CatalogError, "local serving cannot claim federated"):
            self.load_live_bindings(path, catalog, evidence)

        _, target = self.copy_catalog()
        catalog, evidence, qualification, binding = self.federated_molmim_fixture(target)
        federated_digest = qualification["federated_qualification_digest"]
        qualification["federated_qualification_digest"] = None
        path = self.write_binding(target, binding)
        with self.assertRaisesRegex(CatalogError, "requires signed artifact/readiness/semantic"):
            self.load_live_bindings(path, catalog, evidence)
        qualification["federated_qualification_digest"] = federated_digest
        qualification["runtime_tuple_digest"] = hashlib.sha256(
            b"foreign-local-runtime-tuple"
        ).hexdigest()
        path = self.write_binding(target, binding)
        with self.assertRaisesRegex(CatalogError, "cannot mix local B300"):
            self.load_live_bindings(path, catalog, evidence)

    def test_federated_receipt_rejects_runtime_semantic_uri_and_replay_adversaries(self) -> None:
        cases = {
            "runtime-drift": "federated runtime differs",
            "duplicate-response": "two distinct semantic calls",
            "artifact-uri-substitution": "staged artifact differs",
            "replayed-backend-nonce": "nonce was replayed",
        }
        for case, message in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                catalog, evidence, qualification, binding = self.federated_molmim_fixture(
                    target
                )
                digest = qualification["federated_qualification_digest"]
                receipt_path = evidence / "federated-qualifications" / f"{digest}.json"
                receipt = json.loads(receipt_path.read_text())
                if case == "replayed-backend-nonce":
                    backend_digest = qualification["backend_evidence_digest"]
                    backend_attestation = json.loads(
                        (
                            evidence
                            / "attestations"
                            / "backends"
                            / f"{backend_digest}.json"
                        ).read_text()
                    )
                    old_attestation = json.loads(
                        (
                            evidence
                            / "attestations"
                            / "federated-qualifications"
                            / f"{digest}.json"
                        ).read_text()
                    )
                    replay = create_signed_attestation(
                        private_key=self.attestor,
                        session_id=self.session_id,
                        nonce=backend_attestation["nonce"],
                        issued_at="2026-08-26T22:20:00Z",
                        expires_at="2026-08-26T23:00:00Z",
                        kind="federated-qualifications",
                        subject_schema=receipt["schema"],
                        subject_digest=digest,
                        model_id="molmim",
                        claims=old_attestation["claims"],
                    )
                    (
                        evidence
                        / "attestations"
                        / "federated-qualifications"
                        / f"{digest}.json"
                    ).write_text(json.dumps(replay) + "\n")
                else:
                    receipt.pop("receipt_digest")
                    if case == "runtime-drift":
                        receipt["runtime"]["image_digest"] = "sha256:" + hashlib.sha256(
                            b"substituted-runtime"
                        ).hexdigest()
                    elif case == "duplicate-response":
                        receipt["semantic"]["responses"][1]["response_sha256"] = receipt[
                            "semantic"
                        ]["responses"][0]["response_sha256"]
                    else:
                        receipt["artifact"]["content_uri"] = receipt["artifact"][
                            "content_uri"
                        ].replace("/models/molmim/", "/models/other/")
                    replacement_digest, _ = self.write_receipt(
                        evidence,
                        "federated-qualifications",
                        receipt,
                        claims=json.loads(
                            (
                                evidence
                                / "attestations"
                                / "federated-qualifications"
                                / f"{digest}.json"
                            ).read_text()
                        )["claims"],
                    )
                    qualification["federated_qualification_digest"] = replacement_digest
                path = self.write_binding(target, binding)
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_detached_archive_uses_packaged_provenance_without_git_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_root = Path(temporary) / "git-archive"
            target = archive_root / "k8s-inference" / "catalog" / "runtime"
            target.parent.mkdir(parents=True)
            shutil.copytree(CATALOG_ROOT, target)
            canonical = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
            detached = load_catalog(target)
        self.assertEqual(canonical.digest, detached.digest)
        self.assertEqual(tuple(canonical.records), tuple(detached.records))

    def test_runtime_revision_drift_and_dummy_digests_fail_closed(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "qwen3-8b.json"
        value = json.loads(path.read_text())
        content_index = value["runtime"]["command"].index("{FS2_MODEL_CONTENT_PATH}")
        value["runtime"]["command"][content_index] = "Qwen/Qwen3-8B"
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "mounted artifact path"):
            load_catalog(target, repo_root=REPO_ROOT)

        for dummy in ("a" * 64, "deadbeef" * 8, "0123456789abcdef" * 4):
            with self.subTest(dummy=dummy):
                _, target = self.copy_catalog()
                value = self.promote_qwen(
                    target, hashlib.sha256(b"real-manifest").hexdigest()
                )
                value["cache"]["artifact"]["manifest_digest"] = dummy
                (target / "models" / "qwen3-8b.json").write_text(
                    json.dumps(value) + "\n"
                )
                with self.assertRaisesRegex(
                    CatalogError, "placeholder rather than a content digest"
                ):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_completion_and_embedding_protocols_share_the_canonical_paths(self) -> None:
        for protocol, endpoint in (
            ("openai-completions", "/v1/completions"),
            ("openai-embeddings", "/v1/embeddings"),
        ):
            with self.subTest(protocol=protocol):
                _, target = self.copy_catalog()
                path = target / "models" / "qwen3-8b.json"
                value = json.loads(path.read_text())
                value["interface"]["protocols"] = [protocol]
                value["interface"]["endpoints"] = {protocol: endpoint}
                path.write_text(json.dumps(value) + "\n")
                contract_path = target / "contracts" / "semantic-requests.json"
                contracts = json.loads(contract_path.read_text())
                invocation = contracts["contracts"]["qwen3-8b"]["invocation"]
                invocation["protocol"] = protocol
                invocation["endpoint"] = endpoint
                contract_path.write_text(json.dumps(contracts) + "\n")
                index_path = target / "catalog.json"
                index = json.loads(index_path.read_text())
                index["semantic_requests"]["sha256"] = hashlib.sha256(
                    contract_path.read_bytes()
                ).hexdigest()
                index_path.write_text(json.dumps(index) + "\n")
                self.refresh_scale_contract(target, "qwen3-8b")
                loaded = load_catalog(target, repo_root=REPO_ROOT)
                self.assertEqual(
                    [protocol], loaded.model("qwen3-8b").to_dict()["interface"]["protocols"]
                )

    def test_overlay_cannot_replace_artifact_or_model_identity(self) -> None:
        _, target = self.copy_catalog()
        catalog, evidence, qualification, _ = self.live_fixture(target)
        for field, message in (
            ("artifact_manifest_digest", "placeholder rather than a content digest"),
            ("model_digest", "placeholder rather than a content digest"),
        ):
            with self.subTest(field=field):
                value = self.binding_value(catalog, copy.deepcopy(qualification))
                if field == "model_digest":
                    value["bindings"]["qwen3-8b"][field] = "0" * 64
                else:
                    value["bindings"]["qwen3-8b"]["qualification"][field] = "0" * 64
                path = self.write_binding(target, value)
                with self.assertRaisesRegex(CatalogError, message):
                    self.load_live_bindings(path, catalog, evidence)

    def test_reviewed_acceleration_mechanisms_can_become_qualified(self) -> None:
        cases = (("glm-5-2-fp8", "sleep-wake"),)
        for model_id, mechanism in cases:
            with self.subTest(model_id=model_id):
                _, target = self.copy_catalog()
                path = target / "models" / f"{model_id}.json"
                value = json.loads(path.read_text())
                value["resources"]["gpu"]["b300_state"] = "qualified"
                value["startup"]["enabled_mechanisms"] = ["conventional", mechanism]
                experiment = next(
                    item for item in value["startup"]["experiments"] if item["mechanism"] == mechanism
                )
                experiment["state"] = "qualified"
                experiment["artifact_manifest_digest"] = hashlib.sha256(
                    f"{model_id}-qualified-artifact".encode()
                ).hexdigest()
                value["support"]["state"] = "qualified"
                path.write_text(json.dumps(value) + "\n")
                self.refresh_scale_contract(target, model_id)
                catalog = load_catalog(target, repo_root=REPO_ROOT)
                self.assertIn(mechanism, catalog.model(model_id).to_dict()["startup"]["enabled_mechanisms"])

    def test_sm103_incompatible_evo_cannot_be_rewritten_as_qualified(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "evo2-40b.json"
        value = json.loads(path.read_text())
        value["resources"]["gpu"]["b300_state"] = "qualified"
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "immutable compatibility audit"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_gateway_view_is_a_defensive_typed_projection(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        value = {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bindings.json"
            path.write_text(json.dumps(value) + "\n")
            gateway = bind_gateway_catalog(catalog, load_serving_bindings(path, catalog))
        payload = gateway.to_dict()
        payload["models"][0]["routable"] = True
        self.assertEqual((), gateway.routable_model_ids())

    def test_gateway_listing_preserves_medical_license_and_mcp_policy(self) -> None:
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        value = {
            "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
            "catalog_digest": catalog.digest,
            "bindings": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bindings.json"
            path.write_text(json.dumps(value) + "\n")
            gateway = bind_gateway_catalog(catalog, load_serving_bindings(path, catalog))
        cxr = gateway.model("nv-reason-cxr-3b").to_dict()
        self.assertTrue(cxr["policy"]["non_clinical"])
        self.assertEqual("prohibited", cxr["policy"]["commercial_use"])
        self.assertEqual("NVIDIA-OneWay-Noncommercial", cxr["policy"]["license_id"])
        self.assertFalse(cxr["mcp"]["invocable"])
        segment = gateway.model("nv-segment-ct").to_dict()
        self.assertTrue(segment["mcp"]["discoverable"])
        self.assertFalse(segment["mcp"]["invocable"])
        self.assertEqual("qualified", segment["support_state"])


class JsonSchemaTests(unittest.TestCase):
    def test_every_published_json_schema_is_a_valid_draft_2020_12_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for schema validation: {exc}")
        for path in sorted((CATALOG_ROOT / "schema").glob("*.json")):
            with self.subTest(schema=path.name):
                Draft202012Validator.check_schema(json.loads(path.read_text()))

    def test_bound_catalog_contracts_validate_against_published_schemas(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for contract validation: {exc}")
        for contract_name, schema_name in (
            ("artifact-acquisition.json", "artifact-acquisition.schema.json"),
            ("compatibility-audit.json", "compatibility-audit.schema.json"),
            ("federated-backends.json", "federated-backends.schema.json"),
            ("model-variants.json", "model-variants.schema.json"),
            ("runtime-prerequisites.json", "runtime-prerequisites.schema.json"),
            ("scale-contracts.json", "scale-contracts.schema.json"),
            ("semantic-requests.json", "semantic-requests.schema.json"),
        ):
            with self.subTest(contract=contract_name):
                document = json.loads(
                    (CATALOG_ROOT / "contracts" / contract_name).read_text()
                )
                schema = json.loads(
                    (CATALOG_ROOT / "schema" / schema_name).read_text()
                )
                Draft202012Validator(schema).validate(document)

    def test_ngc_materialization_fixture_validates_against_the_published_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for materialization validation: {exc}")
        materialization = make_ngc_materialization(
            {
                "fs2-models/ngc-pull-secret": {
                    "namespace": "fs2-models",
                    "name": "fs2-ngc-pull",
                    "uid": "77777777-7777-7777-7777-777777777777",
                    "resource_version": "31",
                },
                "fs2-models/ngc-runtime-secret": {
                    "namespace": "fs2-models",
                    "name": "fs2-ngc-runtime",
                    "uid": "88888888-8888-8888-8888-888888888888",
                    "resource_version": "32",
                },
            }
        )
        schema = json.loads(
            (
                CATALOG_ROOT
                / "schema"
                / "ngc-credential-materialization.schema.json"
            ).read_text()
        )
        Draft202012Validator(schema).validate(materialization)

    def test_every_model_validates_against_the_published_json_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for catalog validation: {exc}")
        schema = json.loads((CATALOG_ROOT / "schema" / "model.schema.json").read_text())
        validator = Draft202012Validator(schema)
        for path in sorted((CATALOG_ROOT / "models").glob("*.json")):
            with self.subTest(path=path.name):
                validator.validate(json.loads(path.read_text()))

    def test_serving_overlay_schema_accepts_the_closed_empty_route_set(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for overlay validation: {exc}")
        schema = json.loads(
            (CATALOG_ROOT / "schema" / "serving-bindings.schema.json").read_text()
        )
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        Draft202012Validator(schema).validate(
            {
                "schema": "fs2-serve.nebius.ai/serving-bindings/v16",
                "catalog_digest": catalog.digest,
                "bindings": {},
            }
        )

    def test_signed_attestation_schema_accepts_the_canonical_envelope(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - explicit CI dependency gate
            self.fail(f"jsonschema is required for attestation validation: {exc}")
        key = Ed25519PrivateKey.generate()
        attestation = create_signed_attestation(
            private_key=key,
            session_id=hashlib.sha256(b"schema-session").hexdigest(),
            nonce=hashlib.sha256(b"schema-nonce").hexdigest(),
            issued_at="2026-08-26T22:00:00Z",
            expires_at="2026-08-26T23:00:00Z",
            kind="readiness",
            subject_schema="fs2-serve.nebius.ai/readiness-receipt/v2",
            subject_digest=hashlib.sha256(b"readiness-receipt").hexdigest(),
            model_id="qwen3-8b",
            claims={"runtime_tuple_digest": hashlib.sha256(b"runtime").hexdigest()},
        )
        schema = json.loads(
            (CATALOG_ROOT / "schema" / "signed-attestation.schema.json").read_text()
        )
        Draft202012Validator(schema).validate(attestation)
        with self.assertRaisesRegex(CatalogError, "not fresh"):
            verify_signed_attestation(
                attestation,
                trusted_attestors={
                    public_key_id(key.public_key()): public_key_value(key.public_key())
                },
                expected_session_id=attestation["session_id"],
                expected_kind="readiness",
                expected_schema="fs2-serve.nebius.ai/readiness-receipt/v2",
                expected_digest=attestation["subject"]["digest"],
                expected_model_id="qwen3-8b",
                validation_time=datetime(2026, 8, 26, 23, 0, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
