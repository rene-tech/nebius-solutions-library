#!/usr/bin/env python3
"""Verify the signed v3 external execution acknowledgement for Terraform.

This apply-time verifier accepts only the repository-pinned active trust
contract and executor/verifier sources, validates the whole signed short-lived
acknowledgement and exact saved plan, then performs read-only Kubernetes
SelfSubjectReview/SSRR/SSAR checks for the actual unimpersonated platform
transport. It has no mutation API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import audit_sai07_effective_authority_v2 as authority_audit
import sai07_saved_plan_contract as saved_plan
import sai07_authoritative_evidence as evidence
import sai07_epoch_admission_v4 as epoch_admission
import verify_sai07_custody_manifest_bundle as bundle_v1
import verify_sai07_custody_manifest_bundle_v2 as bundle_v2
import verify_sai07_custody_trust_v3 as trust_v3

ROOT = Path("/proc/1/fd/190")
SOURCE_LOCK = Path("/proc/1/fd/183")
CAPSULE_CONTRACT = Path("/proc/1/fd/180")
CAPSULE_TRUST_LOCK = Path("/proc/1/fd/181")
CAPSULE_PLATFORM_AUTHORITY = Path("/proc/1/fd/182")
CAPSULE_RUNTIME_ATTESTATION = Path("/proc/1/fd/184")
CAPSULE_EPOCH_ADMISSION = Path("/proc/1/fd/188")
SCHEMA = "fs2-serve.nebius.ai/sai07-external-execution-acknowledgement/v3"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ZERO_SHA256 = "0" * 64


class AckV3Error(ValueError):
    pass


def exact(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AckV3Error(f"{label} fields differ from the v3 execution contract")
    return value


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise AckV3Error(f"{label} must be a lowercase SHA-256")
    return value


def instant(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AckV3Error(f"{label} must be a UTC RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise AckV3Error(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise AckV3Error(f"{label} is not UTC")
    return parsed


def reconstruct_retained_inventory(
    acknowledgement: dict[str, Any], platform_client: Any
) -> str:
    inventory = acknowledgement["platform_object_inventory"]
    if (
        not isinstance(inventory, list)
        or not inventory
        or inventory != sorted(inventory, key=evidence.canonical)
        or acknowledgement["platform_object_inventory_count"] != str(len(inventory))
    ):
        raise AckV3Error(
            "signed Terraform-retained object inventory is absent or unordered"
        )
    inventory_sha256 = hashlib.sha256(evidence.canonical(inventory)).hexdigest()
    if (
        inventory_sha256 != acknowledgement["platform_objects_before_sha256"]
        or inventory_sha256 != acknowledgement["platform_objects_after_sha256"]
    ):
        raise AckV3Error(
            "signed Terraform-retained object inventory differs from its fences"
        )
    api_paths: set[str] = set()
    identities: set[tuple[str, str, str, str]] = set()
    state_addresses: set[str] = set()
    for index, entry in enumerate(inventory):
        item = exact(
            entry,
            {
                "api_path",
                "identity",
                "object_sha256",
                "present",
                "resource_version",
                "state_address",
                "uid",
            },
            f"retained object inventory {index}",
        )
        identity_value = item["identity"]
        if (
            item["present"] is not True
            or not isinstance(identity_value, list)
            or len(identity_value) != 4
            or not all(isinstance(value, str) for value in identity_value)
            or not identity_value[0]
            or not identity_value[1]
            or not identity_value[3]
            or not isinstance(item["api_path"], str)
            or not isinstance(item["state_address"], str)
            or not item["state_address"]
            or not isinstance(item["resource_version"], str)
            or not item["resource_version"]
            or not isinstance(item["uid"], str)
            or not item["uid"]
        ):
            raise AckV3Error("retained object inventory identity is incomplete")
        identity = tuple(identity_value)
        expected_path = bundle_v2.api_path(identity)
        if item["api_path"] != expected_path:
            raise AckV3Error("retained object inventory API path differs from identity")
        digest(item["object_sha256"], f"retained object inventory {index} SHA-256")
        if (
            item["api_path"] in api_paths
            or identity in identities
            or item["state_address"] in state_addresses
        ):
            raise AckV3Error("retained object inventory contains a duplicate identity")
        api_paths.add(item["api_path"])
        identities.add(identity)
        state_addresses.add(item["state_address"])
        live = platform_client.raw(item["api_path"])
        metadata = live.get("metadata", {})
        if (
            live.get("apiVersion") != identity[0]
            or live.get("kind") != identity[1]
            or metadata.get("namespace", "") != identity[2]
            or metadata.get("name") != identity[3]
            or metadata.get("uid") != item["uid"]
            or metadata.get("resourceVersion") != item["resource_version"]
            or hashlib.sha256(evidence.canonical(live)).hexdigest()
            != item["object_sha256"]
        ):
            raise AckV3Error(
                "live Terraform-retained object differs from the signed inventory"
            )
    return inventory_sha256


def validate_capsule_admission(capsule: dict[str, Any]) -> None:
    admission = exact(
        capsule.get("admission"),
        {
            "pod_security_contract",
            "pod_security_projection",
            "required_objects_by_role",
        },
        "capsule admission contract",
    )
    expected_profile = {
        "automount_service_account_token": False,
        "capabilities_drop_all": True,
        "forbid_capability_additions": True,
        "forbid_host_namespaces": True,
        "forbid_host_path": True,
        "forbid_host_ports": True,
        "forbid_privileged": True,
        "require_allow_privilege_escalation_false": True,
        "require_digest_images": True,
        "require_read_only_root_filesystem": True,
        "require_run_as_non_root": True,
        "required_seccomp_type": "RuntimeDefault",
    }
    roles = exact(
        admission["required_objects_by_role"],
        {"external-ack", "plan-apply"},
        "capsule role admission objects",
    )
    if (
        admission["pod_security_contract"] != expected_profile
        or admission["pod_security_projection"]
        != "canonical-v1-full-spec-and-security-metadata"
        or any(
            not isinstance(paths, list)
            or not paths
            or len(paths) > 8
            or paths != sorted(set(paths))
            or any(
                not isinstance(path, str) or not path.startswith("/api")
                for path in paths
            )
            for paths in roles.values()
        )
    ):
        raise AckV3Error("capsule admission contract is incomplete")


def validate(query: dict[str, str]) -> dict[str, str]:
    supplied_lock = Path(query["trust_lock_path"])
    supplied_capsule = Path(query["execution_capsule_contract_path"])
    if supplied_lock != CAPSULE_TRUST_LOCK or supplied_capsule != CAPSULE_CONTRACT:
        raise AckV3Error("acknowledgement verifier requires the immutable capsule contracts")
    contract_bytes, contract = trust_v3.load_json(
        supplied_lock, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    capsule_bytes, capsule = trust_v3.load_json(
        supplied_capsule,
        "execution capsule contract",
        1024 * 1024,
        repository_document=True,
    )
    capsule_sha256 = hashlib.sha256(capsule_bytes).hexdigest()
    if (
        capsule.get("activation") != "active"
        or capsule.get("schema")
        != "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v4"
        or capsule_sha256
        != digest(
            query["execution_capsule_contract_sha256"],
            "execution capsule contract SHA-256",
        )
        or os.environ.get("FS2_SAI07_CAPSULE_CONTRACT_SHA256") != capsule_sha256
        or os.environ.get("FS2_SAI07_RUNTIME_ATTESTATION_SHA256")
        != query["execution_runtime_attestation_sha256"]
        or os.environ.get("FS2_SAI07_SOURCE_BUNDLE_SHA256")
        != query["execution_source_bundle_sha256"]
    ):
        raise AckV3Error("immutable execution capsule identity is absent or differs")
    validate_capsule_admission(capsule)
    capsule_runtime = exact(
        capsule.get("runtime"),
        {
            "authority_keys",
            "cluster_ca_fd",
            "contract_files",
            "filesystem",
            "handoff_root",
            "plan_variables_fd",
            "platform_kubeconfig_fd",
            "provider_bundle",
            "runtime_files",
            "saved_plan_fd",
            "terraform_configuration_sha256",
            "terraform_data_roots",
            "terraform_data_tree_sha256",
            "terraform_root_tree_sha256",
            "terraform_roots",
            "terraform_version",
        },
        "execution capsule runtime",
    )
    if (
        capsule_runtime["filesystem"] != "observed-read-only-mounts"
        or capsule_runtime["saved_plan_fd"] != 197
        or capsule_runtime["platform_kubeconfig_fd"] != 198
        or capsule_runtime["runtime_files"].get("openssl", {}).get("fd") != 192
        or capsule_runtime["runtime_files"].get("kubectl", {}).get("fd") != 193
        or capsule_runtime["runtime_files"].get("terraform", {}).get("fd") != 194
        or capsule_runtime["runtime_files"].get("source_bundle", {}).get("fd") != 190
        or os.environ.get("FS2_SAI07_SOURCE_BUNDLE_SHA256")
        != capsule_runtime["runtime_files"].get("source_bundle", {}).get("sha256")
    ):
        raise AckV3Error("execution capsule runtime differs from its active contract")
    if contract.get("activation") != "active":
        raise AckV3Error("repository-pinned v3 custody activation is blocked")
    executor = exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "dependency_lock_sha256",
            "field_manager",
            "kubectl_cli_path",
            "kubectl_cli_sha256",
            "owner_token_audience",
            "owner_token_issuer",
            "owner_token_max_seconds",
            "platform_authority_contract_path",
            "platform_authority_contract_sha256",
            "source_bundle_sha256",
            "terraform_cli_path",
            "terraform_cli_sha256",
            "terraform_cli_version",
        },
        "v3 executor pin",
    )
    if (
        executor["owner_token_audience"] != "https://kubernetes.default.svc"
        or executor["owner_token_max_seconds"] != 600
        or not evidence.nonempty(executor["owner_token_issuer"], "owner token issuer")
    ):
        raise AckV3Error("repository owner-token boundary differs from the reviewed contract")
    if (
        executor["source_bundle_sha256"]
        != capsule_runtime["runtime_files"]["source_bundle"]["sha256"]
    ):
        raise AckV3Error("custody trust lock does not select this execution capsule")
    authority_keys = exact(
        capsule_runtime["authority_keys"],
        {"backend", "manifest", "provider"},
        "v4 authority keys",
    )
    for role, contract_role, descriptor in (
        ("manifest", "manifest", 185),
        ("provider", "provider_receipt", 186),
        ("backend", "backend_receipt", 187),
    ):
        authority = exact(
            contract.get("authorities", {}).get(contract_role),
            {
                "key_id",
                "principal_id",
                "protected_resource_ids",
                "public_key_path",
                "public_key_sha256",
                "signing_resource_id",
            },
            f"{role} authority",
        )
        if (
            authority["public_key_path"] != f"/proc/1/fd/{descriptor}"
            or authority["public_key_sha256"] != authority_keys[role]["sha256"]
            or authority_keys[role]["fd"] != descriptor
        ):
            raise AckV3Error(f"{role} authority key is not descriptor sealed")
    dependency_file_bytes = trust_v3.read_regular(
        SOURCE_LOCK, "v3 custody source-lock bytes", 1024 * 1024
    )
    _dependency_document, dependency_lock = trust_v3.load_json(
        SOURCE_LOCK,
        "v3 custody source lock",
        1024 * 1024,
        repository_document=True,
    )
    if hashlib.sha256(dependency_file_bytes).hexdigest() != digest(
        executor["dependency_lock_sha256"], "dependency source-lock SHA-256"
    ):
        raise AckV3Error(
            "custody dependency source lock differs from the repository trust pin"
        )
    exact(
        dependency_lock,
        {"schema", "sources"},
        "v3 custody dependency source lock",
    )
    if dependency_lock["schema"] != "fs2-serve.nebius.ai/sai07-custody-source-lock/v3":
        raise AckV3Error("custody dependency source-lock schema is unsupported")
    exact(dependency_lock["sources"], set(dependency_lock["sources"]), "v3 sources")

    runtime_attestation_bytes = trust_v3.read_regular(
        CAPSULE_RUNTIME_ATTESTATION,
        "sealed runtime attestation",
        8 * 1024 * 1024,
    )
    if hashlib.sha256(runtime_attestation_bytes).hexdigest() != digest(
        query["execution_runtime_attestation_sha256"],
        "runtime attestation SHA-256",
    ):
        raise AckV3Error("sealed runtime attestation differs from the apply query")
    epoch_contract_bytes = trust_v3.read_regular(
        CAPSULE_EPOCH_ADMISSION,
        "sealed epoch admission contract",
        1024 * 1024,
    )
    try:
        epoch_contract = json.loads(epoch_contract_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AckV3Error("epoch admission contract is not JSON") from error
    if epoch_contract_bytes != evidence.canonical(epoch_contract) + b"\n":
        raise AckV3Error("epoch admission contract is not canonical JSON")
    epoch_pin = capsule_runtime.get("contract_files", {}).get("epoch_admission")
    if (
        not isinstance(epoch_pin, dict)
        or epoch_pin.get("fd") != 188
        or epoch_pin.get("sha256")
        != hashlib.sha256(epoch_contract_bytes).hexdigest()
    ):
        raise AckV3Error("epoch admission contract differs from the capsule pin")
    try:
        epoch_admission.render(epoch_contract)
    except epoch_admission.EpochAdmissionError as error:
        raise AckV3Error("epoch admission contract is invalid") from error
    epoch_contract_sha256 = hashlib.sha256(epoch_contract_bytes).hexdigest()

    terraform_path = Path("/proc/1/fd/194")
    kubectl_path = Path("/proc/1/fd/193")
    plan_path = Path(query["actual_saved_plan_path"])
    if (
        plan_path != Path("/proc/1/fd/197")
        or query["platform_kubeconfig_path"] != "/proc/1/fd/198"
    ):
        raise AckV3Error("apply verification requires the capsule's fixed sealed descriptors")
    try:
        apply_plan_contract = saved_plan.inspect_saved_plan(
            plan_path,
            terraform_path,
            digest(capsule_runtime["runtime_files"]["terraform"]["sha256"], "Terraform CLI SHA-256"),
            evidence.nonempty(
                capsule_runtime["terraform_version"], "Terraform CLI version"
            ),
        )
    except saved_plan.SavedPlanError as error:
        raise AckV3Error("apply-time saved plan/config projection is invalid") from error

    platform_authority_bytes = trust_v3.read_regular(
        CAPSULE_PLATFORM_AUTHORITY,
        "platform authority contract",
        32 * 1024 * 1024,
    )
    try:
        platform_authority = json.loads(platform_authority_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AckV3Error("platform authority contract is not JSON") from error
    if (
        not isinstance(platform_authority, dict)
        or platform_authority_bytes
        != evidence.canonical(platform_authority) + b"\n"
    ):
        raise AckV3Error(
            "platform authority contract must be canonical JSON with one terminal LF"
        )
    if hashlib.sha256(platform_authority_bytes).hexdigest() != digest(
        executor["platform_authority_contract_sha256"],
        "platform authority contract SHA-256",
    ):
        raise AckV3Error("platform authority contract differs from the repository pin")
    exact(
        platform_authority,
        {
            "activation",
            "cluster_id",
            "exact_rule_closure",
            "exact_rule_closure_sha256",
            "groups",
            "kube_context",
            "kube_system_uid",
            "namespace_inventory",
            "persistent_volume_names",
            "platform_kubeconfig_sha256",
            "schema",
            "username",
        },
        "platform authority contract",
    )
    if platform_authority["activation"] != "active":
        raise AckV3Error("repository-pinned platform authority contract is blocked")
    if (
        platform_authority["schema"]
        != "fs2-serve.nebius.ai/sai07-platform-authority-contract/v3"
        or platform_authority["cluster_id"] != contract["expected"]["cluster_id"]
        or platform_authority["kube_system_uid"]
        != contract["expected"]["kube_system_uid"]
        or platform_authority["username"]
        != contract["expected"]["platform"]["username"]
        or platform_authority["username"]
        in {
            contract["expected"]["owner"]["username"],
            contract["expected"]["receipt_operator"]["username"],
        }
        or platform_authority["namespace_inventory"]
        != contract["expected"]["namespace_inventory"]
        or platform_authority["persistent_volume_names"]
        != contract["expected"]["persistent_volume_names"]
        or platform_authority["kube_context"] != query["platform_context"]
        or platform_authority["platform_kubeconfig_sha256"]
        != apply_plan_contract["platform_kubeconfig_sha256"]
        or not isinstance(platform_authority["groups"], list)
        or not all(isinstance(group, str) and group for group in platform_authority["groups"])
        or platform_authority["groups"] != sorted(set(platform_authority["groups"]))
        or "system:authenticated" not in platform_authority["groups"]
        or not set(contract["expected"]["platform"]["group_ids"]).issubset(
            platform_authority["groups"]
        )
        or not set(platform_authority["groups"]).isdisjoint(
            set(contract["expected"]["owner"]["group_ids"])
            | set(contract["expected"]["receipt_operator"]["group_ids"])
        )
        or not isinstance(platform_authority["exact_rule_closure"], list)
        or hashlib.sha256(
            evidence.canonical(platform_authority["exact_rule_closure"])
        ).hexdigest()
        != digest(
            platform_authority["exact_rule_closure_sha256"],
            "platform exact-rule closure SHA-256",
        )
    ):
        raise AckV3Error("platform authority contract differs from pinned custody facts")

    ack_bytes, ack = trust_v3.load_json(
        Path(query["ack_path"]),
        "external execution acknowledgement",
        executor["acknowledgement_max_bytes"],
    )
    exact(
        ack,
        {
            "acknowledgement",
            "action",
            "anchor_inventory",
            "authority_key_id",
            "cluster_id",
            "collection_id",
            "consumer",
            "context_sha256",
            "contract_sha256",
            "custody_epoch_generation",
            "custody_epoch_id",
            "custody_epoch_principal_id",
            "custody_epoch_sha256",
            "anchor_inventory_sha256",
            "epoch_admission_contract_sha256",
            "epoch_admission_objects",
            "epoch_admission_objects_sha256",
            "executor_source_sha256",
            "execution_capsule_contract_sha256",
            "execution_plan_runtime_attestation_sha256",
            "execution_runtime_attestation_sha256",
            "execution_source_bundle_sha256",
            "expires_at",
            "issued_at",
            "kube_system_uid",
            "manifest_bundle_sha256",
            "manifest_objects_sha256",
            "owner_authority_after_sha256",
            "owner_authority_before_sha256",
            "owner_token_jti_sha256",
            "phase",
            "platform_authority_contract_sha256",
            "platform_object_inventory",
            "platform_object_inventory_count",
            "platform_objects_after_sha256",
            "platform_objects_before_sha256",
            "platform_plan_contract",
            "platform_plan_contract_sha256",
            "platform_state_all_addresses_sha256",
            "platform_state_all_object_count",
            "platform_state_addresses_sha256",
            "platform_state_objects_sha256",
            "platform_state_lineage",
            "platform_state_serial",
            "platform_state_version",
            "receipt_bundle_sha256",
            "receipt_consumption_sha256",
            "schema",
            "signature",
            "token_anchor",
        },
        "external execution acknowledgement",
    )
    if ack["schema"] != SCHEMA:
        raise AckV3Error("external execution acknowledgement schema is unsupported")
    authority = exact(
        contract["authorities"]["manifest"],
        {
            "key_id",
            "principal_id",
            "protected_resource_ids",
            "public_key_path",
            "public_key_sha256",
            "signing_resource_id",
        },
        "manifest authority",
    )
    key_path = Path(evidence.nonempty(authority["public_key_path"], "manifest public-key path"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    public_key = trust_v3.read_regular(key_path, "manifest authority public key", 65536)
    if hashlib.sha256(public_key).hexdigest() != digest(
        authority["public_key_sha256"], "manifest authority key SHA-256"
    ):
        raise AckV3Error("manifest authority key differs from the repository pin")
    bundle_v1.verify_signature(ack, public_key, authority["key_id"])

    issued = instant(ack["issued_at"], "acknowledgement issued_at")
    expires = instant(ack["expires_at"], "acknowledgement expires_at")
    now = dt.datetime.now(dt.UTC)
    if (
        issued > now + dt.timedelta(seconds=30)
        or now > expires
        or expires <= issued
        or expires - issued > dt.timedelta(minutes=10)
    ):
        raise AckV3Error("external execution acknowledgement is stale or has an excessive lifetime")
    expected = {
        "action": query["expected_action"],
        "cluster_id": query["cluster_id"],
        "consumer": query["expected_consumer"],
        "context_sha256": digest(query["expected_context_sha256"], "expected context SHA-256"),
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "custody_epoch_generation": str(contract["custody_epoch"]["generation"]),
        "custody_epoch_id": contract["custody_epoch"]["epoch_id"],
        "custody_epoch_principal_id": contract["custody_epoch"]["principal_id"],
        "custody_epoch_sha256": digest(
            query["expected_custody_epoch_sha256"],
            "expected custody epoch SHA-256",
        ),
        "executor_source_sha256": executor["source_bundle_sha256"],
        "execution_capsule_contract_sha256": capsule_sha256,
        "execution_plan_runtime_attestation_sha256": digest(
            query["execution_runtime_attestation_sha256"],
            "plan runtime attestation SHA-256",
        ),
        "execution_runtime_attestation_sha256": digest(
            query["execution_external_runtime_attestation_sha256"],
            "external runtime attestation SHA-256",
        ),
        "execution_source_bundle_sha256": digest(
            query["execution_source_bundle_sha256"],
            "source bundle SHA-256",
        ),
        "epoch_admission_contract_sha256": epoch_contract_sha256,
        "kube_system_uid": query["kube_system_uid"],
        "phase": query["expected_phase"],
        "platform_authority_contract_sha256": hashlib.sha256(
            platform_authority_bytes
        ).hexdigest(),
        "receipt_bundle_sha256": digest(query["receipt_bundle_sha256"], "receipt bundle SHA-256"),
    }
    for field, expected_value in expected.items():
        if ack[field] != expected_value:
            raise AckV3Error(f"external execution acknowledgement {field} differs")
    if (
        ack["platform_plan_contract"] != apply_plan_contract
        or ack["platform_plan_contract_sha256"]
        != hashlib.sha256(evidence.canonical(apply_plan_contract)).hexdigest()
        or apply_plan_contract["rollout_phase"] != query["expected_phase"]
        or apply_plan_contract["custody_epoch_sha256"]
        != query["expected_custody_epoch_sha256"]
        or apply_plan_contract["execution_capsule_contract_sha256"]
        != capsule_sha256
        or apply_plan_contract["execution_runtime_attestation_sha256"]
        != query["execution_runtime_attestation_sha256"]
        or apply_plan_contract["execution_external_runtime_attestation_sha256"]
        != query["execution_external_runtime_attestation_sha256"]
        or apply_plan_contract["execution_source_bundle_sha256"]
        != query["execution_source_bundle_sha256"]
        or apply_plan_contract["action"] != query["expected_action"]
        or apply_plan_contract["consumer"] != query["expected_consumer"]
        or apply_plan_contract["context_sha256"] != query["expected_context_sha256"]
        or apply_plan_contract["cluster_id"] != query["cluster_id"]
        or apply_plan_contract["kube_system_uid"] != query["kube_system_uid"]
        or apply_plan_contract["receipt_bundle_sha256"]
        != query["receipt_bundle_sha256"]
        or apply_plan_contract["external_handoff_path_sha256"]
        != hashlib.sha256(query["ack_path"].encode()).hexdigest()
        or apply_plan_contract["platform_kube_context"]
        != query["platform_context"]
        or apply_plan_contract["platform_kubeconfig_path_sha256"]
        != hashlib.sha256(query["platform_kubeconfig_path"].encode()).hexdigest()
        or apply_plan_contract["platform_kubeconfig_sha256"]
        != platform_authority["platform_kubeconfig_sha256"]
    ):
        raise AckV3Error(
            "acknowledgement does not authorize this exact saved plan/config/platform transport"
        )

    try:
        platform_audit = authority_audit.run(
            argparse.Namespace(
                bound_jti_sha256=None,
                cluster_id=query["cluster_id"],
                context=query["platform_context"],
                expected_groups_json=evidence.canonical(
                    platform_authority["groups"]
                ).decode(),
                expected_rule_closure_json=evidence.canonical(
                    platform_authority["exact_rule_closure"]
                ).decode(),
                expected_username=platform_authority["username"],
                kube_system_uid=query["kube_system_uid"],
                kubeconfig=Path(query["platform_kubeconfig_path"]),
                kubectl_path=kubectl_path,
                namespace_inventory_json=evidence.canonical(
                    platform_authority["namespace_inventory"]
                ).decode(),
                persistent_volume_names_json=evidence.canonical(
                    platform_authority["persistent_volume_names"]
                ).decode(),
                profile="platform",
            )
        )
    except authority_audit.AuditError as error:
        raise AckV3Error(
            "apply-time platform identity/effective-authority audit failed"
        ) from error
    if (
        platform_audit.get("schema")
        != "fs2-serve.nebius.ai/sai07-effective-authority-audit/v2"
        or platform_audit.get("profile") != "platform"
        or platform_audit.get("username") != platform_authority["username"]
        or platform_audit.get("groups") != platform_authority["groups"]
        or platform_audit.get("exact_rule_closure_sha256")
        != platform_authority["exact_rule_closure_sha256"]
        or platform_audit.get("transport")
        != {
            "context": query["platform_context"],
            "impersonation_free": True,
            "redacted_projection": True,
        }
    ):
        raise AckV3Error("apply-time platform authority evidence differs from its pin")
    platform_audit_projection = {
        key: value for key, value in platform_audit.items() if key != "observed_at"
    }
    platform_authority_audit_sha256 = hashlib.sha256(
        evidence.canonical(platform_audit_projection)
    ).hexdigest()
    platform_client = authority_audit.Client(
        Path(query["platform_kubeconfig_path"]),
        query["platform_context"],
        kubectl_path,
    )
    for field in (
        "manifest_bundle_sha256",
        "platform_objects_after_sha256",
        "platform_objects_before_sha256",
        "platform_state_addresses_sha256",
        "platform_state_objects_sha256",
        "platform_state_all_addresses_sha256",
        "receipt_consumption_sha256",
        "custody_epoch_sha256",
        "owner_authority_after_sha256",
        "owner_authority_before_sha256",
        "owner_token_jti_sha256",
        "platform_authority_contract_sha256",
        "platform_plan_contract_sha256",
        "anchor_inventory_sha256",
        "epoch_admission_contract_sha256",
        "epoch_admission_objects_sha256",
        "execution_plan_runtime_attestation_sha256",
        "execution_runtime_attestation_sha256",
        "execution_source_bundle_sha256",
        "manifest_objects_sha256",
    ):
        digest(ack[field], field)
    if ack["platform_objects_before_sha256"] != ack["platform_objects_after_sha256"]:
        raise AckV3Error("Terraform-retained objects changed across external acknowledgement SSA")
    if ack["owner_authority_before_sha256"] != ack["owner_authority_after_sha256"]:
        raise AckV3Error("external owner authority changed across acknowledgement SSA")
    anchor_inventory = ack["anchor_inventory"]
    if (
        not isinstance(anchor_inventory, list)
        or hashlib.sha256(evidence.canonical(anchor_inventory)).hexdigest()
        != ack["anchor_inventory_sha256"]
        or [item.get("name") for item in anchor_inventory if isinstance(item, dict)]
        != epoch_contract["retained_anchor_names"]
    ):
        raise AckV3Error("signed metadata-only anchor inventory differs from epoch custody")
    epoch_objects = ack["epoch_admission_objects"]
    if (
        not isinstance(epoch_objects, list)
        or hashlib.sha256(evidence.canonical(epoch_objects)).hexdigest()
        != ack["epoch_admission_objects_sha256"]
    ):
        raise AckV3Error("signed epoch admission object inventory is invalid")
    expected_epoch_manifests = epoch_admission.render(epoch_contract)
    expected_epoch_names = sorted(
        (manifest["apiVersion"], manifest["kind"], manifest["metadata"]["name"])
        for manifest in expected_epoch_manifests
    )
    observed_epoch_names: list[tuple[str, str, str]] = []
    for index, item in enumerate(epoch_objects):
        item = exact(
            item,
            {"api_version", "kind", "name", "object_sha256", "resource_version", "uid"},
            f"epoch admission object {index}",
        )
        for field in ("object_sha256",):
            digest(item[field], f"epoch admission object {index} {field}")
        if not all(
            isinstance(item[field], str) and item[field]
            for field in ("api_version", "kind", "name", "resource_version", "uid")
        ):
            raise AckV3Error("epoch admission object identity is incomplete")
        identity = (item["api_version"], item["kind"], "", item["name"])
        live = platform_client.raw(bundle_v2.api_path(identity))
        metadata = live.get("metadata", {})
        if (
            metadata.get("uid") != item["uid"]
            or metadata.get("resourceVersion") != item["resource_version"]
            or hashlib.sha256(evidence.canonical(live)).hexdigest()
            != item["object_sha256"]
        ):
            raise AckV3Error("live epoch admission object differs from signed identity")
        observed_epoch_names.append((item["api_version"], item["kind"], item["name"]))
    if sorted(observed_epoch_names) != expected_epoch_names:
        raise AckV3Error("live epoch admission inventory differs from the sealed contract")
    if (
        not isinstance(ack["platform_state_all_object_count"], str)
        or not ack["platform_state_all_object_count"].isdigit()
        or int(ack["platform_state_all_object_count"]) < 1
    ):
        raise AckV3Error("complete platform-state object count is invalid")
    if ack["phase"] == "prepare":
        if (
            ack["receipt_bundle_sha256"] != ZERO_SHA256
            or ack["receipt_consumption_sha256"] != ZERO_SHA256
        ):
            raise AckV3Error("prepare acknowledgement unexpectedly claims a phase receipt")
    elif ack["receipt_consumption_sha256"] == ZERO_SHA256:
        raise AckV3Error("post-prepare acknowledgement omits external ledger consumption")

    live_ack = exact(
        ack["acknowledgement"],
        {
            "field_manager",
            "field_set_sha256",
            "name",
            "namespace",
            "object_sha256",
            "resource_version",
            "uid",
        },
        "live acknowledgement identity",
    )
    if (
        live_ack["field_manager"] != executor["field_manager"]
        or live_ack["namespace"] != executor["acknowledgement_namespace"]
        or not isinstance(live_ack["name"], str)
        or not live_ack["name"].startswith(executor["acknowledgement_name_prefix"])
        or not all(
            isinstance(live_ack[field], str) and live_ack[field]
            for field in ("uid", "resource_version")
        )
    ):
        raise AckV3Error(
            "live acknowledgement identity differs from the repository executor contract"
        )
    for field in ("field_set_sha256", "object_sha256"):
        digest(live_ack[field], f"acknowledgement {field}")
    live_ack_object = platform_client.raw(
        bundle_v2.api_path(("v1", "ConfigMap", live_ack["namespace"], live_ack["name"]))
    )
    live_ack_metadata = live_ack_object.get("metadata", {})
    if (
        live_ack_metadata.get("uid") != live_ack["uid"]
        or live_ack_metadata.get("resourceVersion") != live_ack["resource_version"]
        or hashlib.sha256(evidence.canonical(live_ack_object)).hexdigest()
        != live_ack["object_sha256"]
    ):
        raise AckV3Error("live acknowledgement changed after signed post-SSA read")
    token_anchor = exact(
        ack["token_anchor"],
        {
            "custody_epoch_sha256",
            "name",
            "namespace",
            "resource_version",
            "uid",
        },
        "metadata-only token-anchor identity",
    )
    if (
        token_anchor["name"]
        != f"fs2-pod-security-token-anchor-v4-{ack['custody_epoch_sha256']}"
        or token_anchor["namespace"] != "fs2-system"
        or token_anchor["custody_epoch_sha256"] != ack["custody_epoch_sha256"]
        or not all(
            isinstance(token_anchor[field], str) and token_anchor[field]
            for field in ("uid", "resource_version")
        )
    ):
        raise AckV3Error("metadata-only token-anchor identity differs")
    current_anchor_entries = [
        item
        for item in anchor_inventory
        if isinstance(item, dict) and item.get("name") == token_anchor["name"]
    ]
    if len(current_anchor_entries) != 1 or current_anchor_entries[0] != {
        key: token_anchor[key]
        for key in ("custody_epoch_sha256", "name", "resource_version", "uid")
    }:
        raise AckV3Error(
            "signed anchor inventory does not contain the exact current anchor identity"
        )
    # Keep the complete retained-object reconstruction as the final API fence:
    # the caller invokes this verifier immediately before and after the bounded
    # apply, so no aggregate-only or stale external observation is accepted.
    retained_inventory_sha256 = reconstruct_retained_inventory(
        ack, platform_client
    )
    if ack["authority_key_id"] != authority["key_id"]:
        raise AckV3Error("acknowledgement authority key differs from the repository pin")
    return {
        "acknowledgement_name": live_ack["name"],
        "acknowledgement_sha256": hashlib.sha256(ack_bytes).hexdigest(),
        "action": ack["action"],
        "bundle_sha256": ack["receipt_bundle_sha256"],
        "consumer": ack["consumer"],
        "custody_epoch_id": ack["custody_epoch_id"],
        "custody_epoch_sha256": ack["custody_epoch_sha256"],
        "execution_capsule_contract_sha256": ack[
            "execution_capsule_contract_sha256"
        ],
        "execution_plan_runtime_attestation_sha256": ack[
            "execution_plan_runtime_attestation_sha256"
        ],
        "execution_runtime_attestation_sha256": ack[
            "execution_runtime_attestation_sha256"
        ],
        "execution_source_bundle_sha256": ack["execution_source_bundle_sha256"],
        "cluster_id": ack["cluster_id"],
        "context_sha256": ack["context_sha256"],
        "kube_system_uid": ack["kube_system_uid"],
        "phase": ack["phase"],
        "platform_authority_audit_sha256": platform_authority_audit_sha256,
        "platform_plan_contract_sha256": ack["platform_plan_contract_sha256"],
        "platform_object_inventory_sha256": retained_inventory_sha256,
        "lease_expires_at": ack["expires_at"],
        "verified_at": dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "valid": "true",
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "ack_path",
            "actual_saved_plan_path",
            "cluster_id",
            "execution_capsule_contract_path",
            "execution_capsule_contract_sha256",
            "execution_external_runtime_attestation_sha256",
            "execution_runtime_attestation_sha256",
            "execution_source_bundle_sha256",
            "expected_action",
            "expected_consumer",
            "expected_context_sha256",
            "expected_custody_epoch_sha256",
            "expected_phase",
            "kube_system_uid",
            "platform_context",
            "platform_kubeconfig_path",
            "receipt_bundle_sha256",
            "trust_lock_path",
        }
        if (
            not isinstance(query, dict)
            or set(query) != required
            or not all(isinstance(query[field], str) for field in required)
        ):
            raise AckV3Error("external query fields differ from the v3 acknowledgement contract")
        result = validate(query)
    except (
        AckV3Error,
        authority_audit.AuditError,
        bundle_v1.BundleError,
        evidence.EvidenceError,
        saved_plan.SavedPlanError,
        trust_v3.TrustV3Error,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"SAI-07 external execution acknowledgement rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
