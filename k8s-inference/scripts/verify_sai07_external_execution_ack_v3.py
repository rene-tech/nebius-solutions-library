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
import verify_sai07_custody_manifest_bundle as bundle_v1
import verify_sai07_custody_trust_v3 as trust_v3

ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"
SOURCE_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-source-lock-v3.json"
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


def validate(query: dict[str, str]) -> dict[str, str]:
    supplied_lock = Path(query["trust_lock_path"])
    if supplied_lock.resolve() != TRUST_LOCK.resolve():
        raise AckV3Error("acknowledgement verifier requires the repository-pinned v3 lock")
    contract_bytes, contract = trust_v3.load_json(
        supplied_lock, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    if contract.get("activation") != "active":
        raise AckV3Error("repository-pinned v3 custody activation is blocked")
    executor = exact(
        contract.get("executor"),
        {
            "acknowledgement_max_bytes",
            "acknowledgement_name_prefix",
            "acknowledgement_namespace",
            "authority_audit_path",
            "authority_audit_sha256",
            "dependency_lock_sha256",
            "field_manager",
            "kubectl_cli_path",
            "kubectl_cli_sha256",
            "owner_token_audience",
            "owner_token_issuer",
            "owner_token_max_seconds",
            "platform_authority_contract_path",
            "platform_authority_contract_sha256",
            "secret_transport_path",
            "secret_transport_sha256",
            "source_path",
            "source_sha256",
            "terraform_cli_path",
            "terraform_cli_sha256",
            "terraform_cli_version",
            "verifier_path",
            "verifier_sha256",
        },
        "v3 executor pin",
    )
    if (
        executor["owner_token_audience"] != "https://kubernetes.default.svc"
        or executor["owner_token_max_seconds"] != 600
        or not evidence.nonempty(executor["owner_token_issuer"], "owner token issuer")
    ):
        raise AckV3Error("repository owner-token boundary differs from the reviewed contract")
    source_relative = Path(evidence.nonempty(executor["source_path"], "executor source path"))
    verifier_relative = Path(evidence.nonempty(executor["verifier_path"], "ack verifier path"))
    audit_relative = Path(
        evidence.nonempty(executor["authority_audit_path"], "authority audit path")
    )
    transport_relative = Path(
        evidence.nonempty(executor["secret_transport_path"], "Secret transport path")
    )
    platform_authority_relative = Path(
        evidence.nonempty(
            executor["platform_authority_contract_path"],
            "platform authority contract path",
        )
    )
    if (
        source_relative.is_absolute()
        or verifier_relative.is_absolute()
        or audit_relative.is_absolute()
        or transport_relative.is_absolute()
        or platform_authority_relative.is_absolute()
        or ".." in source_relative.parts
        or ".." in verifier_relative.parts
        or ".." in audit_relative.parts
        or ".." in transport_relative.parts
        or ".." in platform_authority_relative.parts
    ):
        raise AckV3Error("executor source pins must be repository-relative without traversal")
    source = ROOT / source_relative
    verifier = ROOT / verifier_relative
    audit = ROOT / audit_relative
    transport = ROOT / transport_relative
    if verifier.resolve() != Path(__file__).resolve():
        raise AckV3Error("repository contract selects another acknowledgement verifier")
    for path, expected, label in (
        (source, executor["source_sha256"], "executor"),
        (verifier, executor["verifier_sha256"], "ack verifier"),
        (audit, executor["authority_audit_sha256"], "authority audit"),
        (transport, executor["secret_transport_sha256"], "Secret transport"),
    ):
        actual = hashlib.sha256(
            trust_v3.read_regular(path, f"{label} source", 4 * 1024 * 1024)
        ).hexdigest()
        if actual != digest(expected, f"{label} source SHA-256"):
            raise AckV3Error(f"{label} differs from the repository-pinned source")
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
    expected_dependencies = {
        "authoritative_evidence": "scripts/sai07_authoritative_evidence.py",
        "custody_manifest_v1": "scripts/verify_sai07_custody_manifest_bundle.py",
        "custody_manifest_v2": "scripts/verify_sai07_custody_manifest_bundle_v2.py",
        "custody_manifest_v3": "scripts/verify_sai07_custody_manifest_bundle_v3.py",
        "custody_preflight_v3": "scripts/run_sai07_retained_state_custody_v3.py",
        "custody_state_semantics": "scripts/sai07_custody_state_semantics.py",
        "custody_trust_v2": "scripts/verify_sai07_custody_trust.py",
        "custody_trust_v3": "scripts/verify_sai07_custody_trust_v3.py",
        "effective_authority_v1": "scripts/audit_sai07_effective_authority.py",
        "receipt_transition": "scripts/verify_pod_security_receipts.py",
        "saved_plan_contract": "scripts/sai07_saved_plan_contract.py",
        "secret_metadata_transport": "scripts/collect_sai07_secret_metadata.py",
    }
    sources = exact(
        dependency_lock["sources"],
        set(expected_dependencies),
        "v3 custody dependency sources",
    )
    for label, expected_path in expected_dependencies.items():
        pin = exact(sources[label], {"path", "sha256"}, f"{label} pin")
        if pin["path"] != expected_path:
            raise AckV3Error(f"{label} source path differs from the closed contract")
        actual = hashlib.sha256(
            trust_v3.read_regular(
                ROOT / expected_path, f"{label} source", 4 * 1024 * 1024
            )
        ).hexdigest()
        if actual != digest(pin["sha256"], f"{label} source SHA-256"):
            raise AckV3Error(
                f"{label} differs from the repository-pinned dependency source"
            )

    terraform_path = Path(
        evidence.nonempty(executor["terraform_cli_path"], "Terraform CLI path")
    )
    kubectl_path = Path(
        evidence.nonempty(executor["kubectl_cli_path"], "kubectl CLI path")
    )
    if not kubectl_path.is_absolute() or ".." in kubectl_path.parts:
        raise AckV3Error("kubectl CLI path must be absolute without traversal")
    if hashlib.sha256(
        trust_v3.read_regular(kubectl_path, "kubectl CLI", 256 * 1024 * 1024)
    ).hexdigest() != digest(
        executor["kubectl_cli_sha256"], "kubectl CLI SHA-256"
    ):
        raise AckV3Error("kubectl CLI differs from the repository pin")
    plan_path_raw = os.environ.get("FS2_SAI07_APPLY_PLAN_PATH")
    if plan_path_raw is None:
        raise AckV3Error("apply-time saved-plan path is absent")
    plan_path = Path(plan_path_raw)
    try:
        apply_plan_contract = saved_plan.inspect_saved_plan(
            plan_path,
            terraform_path,
            digest(executor["terraform_cli_sha256"], "Terraform CLI SHA-256"),
            evidence.nonempty(executor["terraform_cli_version"], "Terraform CLI version"),
        )
    except saved_plan.SavedPlanError as error:
        raise AckV3Error("apply-time saved plan/config projection is invalid") from error

    platform_authority_bytes = trust_v3.read_regular(
        ROOT / platform_authority_relative,
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
            "executor_source_sha256",
            "expires_at",
            "issued_at",
            "kube_system_uid",
            "manifest_bundle_sha256",
            "owner_authority_after_sha256",
            "owner_authority_before_sha256",
            "owner_token_jti_sha256",
            "phase",
            "platform_authority_contract_sha256",
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
        "executor_source_sha256": executor["source_sha256"],
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
        or apply_plan_contract["external_handoff_path_sha256"]
        != hashlib.sha256(query["ack_path"].encode()).hexdigest()
        or apply_plan_contract["platform_kube_context"]
        != query["platform_context"]
        or apply_plan_contract["platform_kubeconfig_path_sha256"]
        != hashlib.sha256(query["platform_kubeconfig_path"].encode()).hexdigest()
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
    ):
        digest(ack[field], field)
    if ack["platform_objects_before_sha256"] != ack["platform_objects_after_sha256"]:
        raise AckV3Error("Terraform-retained objects changed across external acknowledgement SSA")
    if ack["owner_authority_before_sha256"] != ack["owner_authority_after_sha256"]:
        raise AckV3Error("external owner authority changed across acknowledgement SSA")
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
        != f"fs2-pod-security-token-anchor-v3-{ack['custody_epoch_sha256']}"
        or token_anchor["namespace"] != "fs2-system"
        or token_anchor["custody_epoch_sha256"] != ack["custody_epoch_sha256"]
        or not all(
            isinstance(token_anchor[field], str) and token_anchor[field]
            for field in ("uid", "resource_version")
        )
    ):
        raise AckV3Error("metadata-only token-anchor identity differs")
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
        "phase": ack["phase"],
        "platform_authority_audit_sha256": platform_authority_audit_sha256,
        "platform_plan_contract_sha256": ack["platform_plan_contract_sha256"],
        "valid": "true",
    }


def main() -> int:
    try:
        raw_query = os.environ.get("FS2_SAI07_APPLY_QUERY")
        query = json.loads(raw_query) if raw_query is not None else json.load(sys.stdin)
        required = {
            "ack_path",
            "cluster_id",
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
