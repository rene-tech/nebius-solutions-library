from __future__ import annotations

import copy
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "stages/foundation/scripts/verify-public-edge-node-eligibility.py"
)
SPEC = importlib.util.spec_from_file_location("public_edge_apply_eligibility", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

CLUSTER_ID = "mk8scluster-test123"
GROUP_ID = "mk8snodegroup-test123"
RUN_ID = "r12345"
SELECTOR = {
    "workload.fs2.nebius/system": "true",
    "capacity.fs2.nebius/type": "regular",
    "capacity.fs2.nebius/pool": "system",
    "lifecycle.fs2.nebius/run": RUN_ID,
    "nebius.com/node-group-id": GROUP_ID,
}
KUBECONFIG_SHA256 = "d" * 64
TEST_ED25519_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc4"
    "4449c5697b326919703bac031cae7f60"
)
TEST_ED25519_PUBLIC_KEY = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a"
    "0ee172f3daa62325af021a68f707511a"
)


def sign_ed25519(message: bytes) -> bytes:
    private_der = bytes.fromhex("302e020100300506032b657004220420") + TEST_ED25519_SEED
    encoded = base64.b64encode(private_der).decode("ascii")
    private_pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(
            encoded[index : index + 64] for index in range(0, len(encoded), 64)
        )
        + "\n-----END PRIVATE KEY-----\n"
    ).encode("ascii")
    key_fd = GATE.sealed_memfd("test-ed25519-key", private_pem)
    message_fd = GATE.sealed_memfd("test-ed25519-message", message)
    try:
        result = subprocess.run(
            [
                GATE.openssl_binary(),
                "pkeyutl",
                "-sign",
                "-inkey",
                f"/proc/self/fd/{key_fd}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{message_fd}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            pass_fds=(key_fd, message_fd),
            timeout=15,
        )
    finally:
        os.close(key_fd)
        os.close(message_fd)
    assert result.returncode == 0
    assert len(result.stdout) == 64
    return result.stdout


def der_value(tag: int, content: bytes) -> bytes:
    if len(content) < 128:
        encoded_length = bytes([len(content)])
    else:
        width = (len(content).bit_length() + 7) // 8
        encoded_length = bytes([0x80 | width]) + len(content).to_bytes(width, "big")
    return bytes([tag]) + encoded_length + content


def deterministic_test_ca_der() -> bytes:
    ed25519_algorithm = bytes.fromhex("300506032b6570")
    common_name = der_value(
        0x30,
        bytes.fromhex("0603550403") + der_value(0x0C, b"FS2 deterministic test CA"),
    )
    distinguished_name = der_value(0x30, der_value(0x31, common_name))
    validity = der_value(
        0x30,
        der_value(0x17, b"260101000000Z")
        + der_value(0x17, b"351231235959Z"),
    )
    subject_public_key = der_value(
        0x30,
        ed25519_algorithm + der_value(0x03, b"\x00" + TEST_ED25519_PUBLIC_KEY),
    )
    basic_constraints = der_value(
        0x30,
        bytes.fromhex("0603551d13")
        + bytes.fromhex("0101ff")
        + der_value(0x04, bytes.fromhex("30030101ff")),
    )
    key_usage = der_value(
        0x30,
        bytes.fromhex("0603551d0f")
        + bytes.fromhex("0101ff")
        + der_value(0x04, bytes.fromhex("03020106")),
    )
    extensions = der_value(0xA3, der_value(0x30, basic_constraints + key_usage))
    tbs_certificate = der_value(
        0x30,
        bytes.fromhex("a003020102")
        + bytes.fromhex("020101")
        + ed25519_algorithm
        + distinguished_name
        + validity
        + distinguished_name
        + subject_public_key
        + extensions,
    )
    signature = sign_ed25519(tbs_certificate)
    return der_value(
        0x30,
        tbs_certificate
        + ed25519_algorithm
        + der_value(0x03, b"\x00" + signature),
    )


def node_group(resource_version: str = "11") -> dict[str, object]:
    return {
        "metadata": {
            "id": GROUP_ID,
            "parent_id": CLUSTER_ID,
            "resource_version": resource_version,
        },
        "spec": {
            "fixed_node_count": 3,
            "template": {
                "metadata": {
                    "labels": {
                        key: value
                        for key, value in SELECTOR.items()
                        if key != "nebius.com/node-group-id"
                    }
                }
            },
        },
        "status": {
            "state": "RUNNING",
            "target_node_count": 3,
            "node_count": 3,
            "outdated_node_count": 0,
            "ready_node_count": 3,
            "reconciling": False,
        },
    }


def complete_list(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "items": items,
        "pagination": {
            "complete": True,
            "page_count": 1,
            "item_count": len(items),
            "terminal_next_page_token": "",
        },
    }


def provider_instance(ordinal: int, resource_version: str | None = None) -> dict[str, object]:
    instance_id = f"computeinstance-test{ordinal}"
    return {
        "metadata": {
            "id": instance_id,
            "parent_id": "project-test123",
            # Deliberately not a NodeGroup-derived name. Membership authority
            # comes from the signed provider relation, never this convention.
            "name": f"managed-system-member-{ordinal}",
            "resource_version": resource_version or str(20 + ordinal),
            "created_at": f"2026-09-17T11:0{ordinal}:00Z",
        },
        "spec": {"stopped": False},
        "status": {"state": "RUNNING", "reconciling": False},
    }


def provider_instances() -> dict[str, object]:
    return complete_list([provider_instance(ordinal) for ordinal in range(1, 4)])


def provider_instance_gets() -> dict[str, dict[str, object]]:
    return {
        f"computeinstance-test{ordinal}": provider_instance(ordinal)
        for ordinal in range(1, 4)
    }


def provider_member_set() -> set[str]:
    return {f"computeinstance-test{ordinal}" for ordinal in range(1, 4)}


def membership_subject() -> dict[str, object]:
    return GATE.membership_subject(
        project_id="project-test123",
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        expected_count=3,
        minimum_domains=3,
        maximum_surge=1,
        selector=SELECTOR,
        kubeconfig_sha256=KUBECONFIG_SHA256,
    )


def membership_receipt() -> tuple[
    dict[str, object], dict[str, object], dict[str, object], bytes
]:
    epoch = {
        "sequence": 1,
        "phase": "stable",
        "predecessor_payload_sha256": "0" * 64,
        "serving_member_instance_ids": sorted(provider_member_set()),
        "joining_member_instance_ids": [],
        "retiring_member_instance_ids": [],
        "admitted_member_instance_ids": sorted(provider_member_set()),
    }
    observer = {
        "id": "provider-observer-test",
        "provider": "nebius",
        "endpoint": "https://api.nebius.cloud",
        "credential_authority": "nebius-workload-identity",
        "credential_subject": "serviceaccount-observer123",
        "audience": "public-edge-membership",
        "configuration_sha256": "f" * 64,
        "adapter_sha256": "a" * 64,
    }
    evidence = {
        "schema": "fs2-serve.nebius.ai/provider-node-group-membership-export/v2",
        "provider": "nebius",
        "project_id": "project-test123",
        "cluster_id": CLUSTER_ID,
        "node_group_id": GROUP_ID,
        "node_group_resource_version": "11",
        "relation_api": "nebius-managed-kubernetes-node-group-membership/v1",
        "member_instance_ids": sorted(provider_member_set()),
        "membership_epoch": epoch,
        "provider_observer": observer,
        "collected_at": "2026-09-17T12:00:00Z",
        "adapter_sha256": "a" * 64,
    }
    evidence_raw = GATE.canonical_bytes(evidence) + b"\n"
    key = b"k" * 32
    key_id = "sha256:" + hashlib.sha256(key).hexdigest()
    toolchain = {
        name: {
            "path": f"/usr/bin/{name}",
            "resolved_path": f"/usr/bin/{name}",
            "sha256": "b" * 64,
        }
        for name in ("python3", "provider_observer", "kubectl")
    }
    payload = {
        "schema": GATE.MEMBERSHIP_PAYLOAD_SCHEMA,
        "issuer": {
            "id": "provider-membership-review",
            "role": GATE.MEMBERSHIP_ISSUER_ROLE,
            "key_id": key_id,
        },
        "nonce": "c" * 64,
        "issued_at": "2026-09-17T12:00:00Z",
        "expires_at": "2026-09-17T13:00:00Z",
        "subject": membership_subject(),
        "provider_membership": {
            "relation_api": "nebius-managed-kubernetes-node-group-membership/v1",
            "node_group_resource_version": "11",
            "member_instance_ids": sorted(provider_member_set()),
            "membership_epoch": epoch,
            "provider_observer": observer,
        },
        "toolchain": toolchain,
        "evidence": {
            "provider_membership_export_sha256": hashlib.sha256(evidence_raw).hexdigest()
        },
    }
    receipt = {
        "schema": GATE.MEMBERSHIP_RECEIPT_SCHEMA,
        "algorithm": "ed25519",
        "payload": payload,
        "payload_sha256": hashlib.sha256(GATE.canonical_bytes(payload)).hexdigest(),
        "signature": base64.urlsafe_b64encode(b"s" * 64).rstrip(b"=").decode("ascii"),
    }
    trust = {
        "schema": GATE.MEMBERSHIP_TRUST_SCHEMA,
        "issuers": [
            {
                "id": "provider-membership-review",
                "role": GATE.MEMBERSHIP_ISSUER_ROLE,
                "key_id": key_id,
                "public_key": base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii"),
            }
        ],
    }
    adapter_trust = {
        "schema": GATE.PROVIDER_ADAPTER_TRUST_SCHEMA,
        "adapters": [
            {
                **observer,
                "executable_sha256": "b" * 64,
            }
        ],
    }
    return receipt, trust, adapter_trust, evidence_raw


def node(ordinal: int) -> dict[str, object]:
    instance_id = f"computeinstance-test{ordinal}"
    return {
        "metadata": {
            "name": instance_id,
            "uid": f"00000000-0000-4000-8000-00000000000{ordinal}",
            "resourceVersion": str(100 + ordinal),
            "annotations": {
                "cluster.x-k8s.io/cluster-name": CLUSTER_ID,
                "cluster.x-k8s.io/owner-kind": "MachineSet",
                "cluster.x-k8s.io/owner-name": GROUP_ID,
                "cluster.x-k8s.io/machine": f"{GROUP_ID}-node{ordinal}",
            },
            "labels": {
                **SELECTOR,
                "kubernetes.io/hostname": instance_id,
            },
        },
        "spec": {
            "providerID": f"nebius://{instance_id}",
            "taints": [],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def node_list(resource_version: str = "200") -> dict[str, object]:
    return {
        "metadata": {"resourceVersion": resource_version},
        "items": [node(ordinal) for ordinal in range(1, 4)],
    }


def test_current_provider_group_and_three_owned_ready_nodes_are_accepted() -> None:
    group = node_group()
    revision, digest = GATE.validate_node_group(
        complete_list([copy.deepcopy(group)]),
        complete_list([copy.deepcopy(group)]),
        copy.deepcopy(group),
        copy.deepcopy(group),
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        expected_count=3,
        maximum_surge=1,
        membership_phase="stable",
        selector=SELECTOR,
    )
    node_revision, count, domains, node_digest = GATE.validate_nodes(
        node_list("200"),
        node_list("201"),
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        selector=SELECTOR,
        provider_member_ids=provider_member_set(),
        serving_member_ids=provider_member_set(),
        expected_count=3,
        minimum_domains=3,
    )

    assert revision == "11"
    assert len(digest) == 64
    assert node_revision == "201"
    assert count == domains == 3
    assert len(node_digest) == 64


def test_prepare_epoch_keeps_old_serving_set_while_joiner_initializes() -> None:
    joining_id = "computeinstance-test4"
    partial_joiner = {
        "metadata": {
            "name": joining_id,
            "uid": "00000000-0000-4000-8000-000000000004",
            "resourceVersion": "104",
            "annotations": {},
            "labels": {"kubernetes.io/hostname": joining_id},
        },
        "spec": {"taints": []},
        "status": {"conditions": []},
    }
    before = node_list("200")
    before["items"].append(copy.deepcopy(partial_joiner))
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "201"
    _revision, count, domains, _digest = GATE.validate_nodes(
        before,
        after,
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        selector=SELECTOR,
        provider_member_ids={*provider_member_set(), joining_id},
        serving_member_ids=provider_member_set(),
        expected_count=3,
        minimum_domains=3,
        joining_member_ids={joining_id},
    )
    assert count == domains == 3


def test_provider_members_are_enumerated_from_compute_and_exact_gets() -> None:
    members, digest = GATE.validate_provider_members(
        provider_instances(),
        provider_instance_gets(),
        provider_instances(),
        provider_instance_gets(),
        project_id="project-test123",
        signed_instance_ids=sorted(provider_member_set()),
        expected_count=3,
        maximum_surge=1,
    )
    assert members == provider_member_set()
    assert len(digest) == 64


def test_signed_provider_relation_is_the_only_membership_authority(monkeypatch) -> None:
    receipt, trust, adapter_trust, evidence_raw = membership_receipt()
    evidence = json.loads(evidence_raw)
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    monkeypatch.setattr(
        GATE,
        "checked_executable",
        lambda _record, name: sys.executable if name == "python3" else f"/usr/bin/{name}",
    )

    result = GATE.validate_membership_receipt(
        receipt,
        trust,
        adapter_trust,
        membership_subject(),
        evidence,
        hashlib.sha256(evidence_raw).hexdigest(),
        validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
    )

    assert result["provider_member_instance_ids"] == sorted(provider_member_set())
    assert result["node_group_resource_version"] == "11"
    assert result["phase"] == "stable"
    assert result["serving_member_instance_ids"] == sorted(provider_member_set())
    assert result["provider_observer"]["adapter_sha256"] == "a" * 64


def test_prepare_epoch_keeps_all_serving_nodes_and_admits_only_bounded_surge() -> None:
    serving = sorted(provider_member_set())
    joining = ["computeinstance-test4"]
    epoch = GATE.validate_membership_epoch(
        {
            "sequence": 2,
            "phase": "prepare",
            "predecessor_payload_sha256": "d" * 64,
            "serving_member_instance_ids": serving,
            "joining_member_instance_ids": joining,
            "retiring_member_instance_ids": [],
            "admitted_member_instance_ids": sorted([*serving, *joining]),
        },
        expected_count=3,
        maximum_surge=1,
        provider_member_ids=sorted([*serving, *joining]),
    )
    assert epoch["phase"] == "prepare"
    assert epoch["serving_member_instance_ids"] == serving
    assert len(epoch["epoch_id"]) == 64


def test_cutover_epoch_retains_old_member_for_quiescence_and_rollback() -> None:
    serving = [
        "computeinstance-test2",
        "computeinstance-test3",
        "computeinstance-test4",
    ]
    retiring = ["computeinstance-test1"]
    epoch = GATE.validate_membership_epoch(
        {
            "sequence": 3,
            "phase": "cutover",
            "predecessor_payload_sha256": "e" * 64,
            "serving_member_instance_ids": serving,
            "joining_member_instance_ids": [],
            "retiring_member_instance_ids": retiring,
            "admitted_member_instance_ids": sorted([*serving, *retiring]),
        },
        expected_count=3,
        maximum_surge=1,
        provider_member_ids=sorted([*serving, *retiring]),
    )
    assert epoch["retiring_member_instance_ids"] == retiring


def test_transition_epoch_cannot_drop_a_serving_domain_or_exceed_surge() -> None:
    serving = sorted(provider_member_set())
    with pytest.raises(GATE.GateError, match="bounded joining surge"):
        GATE.validate_membership_epoch(
            {
                "sequence": 2,
                "phase": "prepare",
                "predecessor_payload_sha256": "f" * 64,
                "serving_member_instance_ids": serving,
                "joining_member_instance_ids": [
                    "computeinstance-test4",
                    "computeinstance-test5",
                ],
                "retiring_member_instance_ids": [],
                "admitted_member_instance_ids": sorted(
                    [*serving, "computeinstance-test4", "computeinstance-test5"]
                ),
            },
            expected_count=3,
            maximum_surge=1,
            provider_member_ids=sorted(
                [*serving, "computeinstance-test4", "computeinstance-test5"]
            ),
        )


def test_apply_time_epoch_cas_rejects_second_saved_plan_from_same_predecessor() -> None:
    """Model the oldObject comparisons embedded in the immutable CAS policy."""

    epoch_n = {
        "sequence": "7",
        "payload": "a" * 64,
        "phase": "stable",
        "serving": '["computeinstance-test1","computeinstance-test2","computeinstance-test3"]',
        "joining": "[]",
        "retiring": "[]",
    }

    def successor(payload: str, joining: str) -> dict[str, str]:
        return {
            "sequence": "8",
            "payload": payload,
            "predecessor_payload": epoch_n["payload"],
            "predecessor_phase": epoch_n["phase"],
            "predecessor_serving": epoch_n["serving"],
            "predecessor_joining": epoch_n["joining"],
            "predecessor_retiring": epoch_n["retiring"],
            "phase": "prepare",
            "serving": epoch_n["serving"],
            "joining": joining,
            "retiring": "[]",
        }

    def apply_time_cas(old: dict[str, str], new: dict[str, str]) -> bool:
        return (
            int(new["sequence"]) == int(old["sequence"]) + 1
            and new["predecessor_payload"] == old["payload"]
            and new["predecessor_phase"] == old["phase"]
            and new["predecessor_serving"] == old["serving"]
            and new["predecessor_joining"] == old["joining"]
            and new["predecessor_retiring"] == old["retiring"]
        )

    plan_a = successor("b" * 64, '["computeinstance-test4"]')
    plan_b = successor("c" * 64, '["computeinstance-test5"]')
    assert apply_time_cas(epoch_n, plan_a)
    installed_a = {
        "sequence": plan_a["sequence"],
        "payload": plan_a["payload"],
        "phase": plan_a["phase"],
        "serving": plan_a["serving"],
        "joining": plan_a["joining"],
        "retiring": plan_a["retiring"],
    }
    assert not apply_time_cas(installed_a, plan_b)


def test_external_admission_binds_exact_dynamic_policy_and_actor() -> None:
    source = (ROOT / "stages/foundation/public_edge_cas_bootstrap.tf").read_text(
        encoding="utf-8"
    )
    assert "paramKind" in source
    assert 'parameterNotFoundAction = "Deny"' in source
    assert "object.spec == params.spec.policySpec" in source
    assert "object.metadata.annotations == params.spec.policyAnnotations" in source
    assert "public_edge_cas_bootstrap_creator_cel" in source
    assert "rbac_review_sha256 != strrep" in source
    assert 'kind == "provider-iam+apiserver-admission"' in source
    assert "public_edge_required_identity_paths" in source
    assert "provenance_attestation_sha256" in source
    assert "controller_image_digest" in source
    assert "spec.preventiveBoundary" in source
    assert source.count(
        "(request.operation == 'DELETE' ? oldObject.metadata.name : object.metadata.name)"
    ) == 2
    assert (
        "request.resource.resource != 'validatingadmissionpolicies' || "
        "(request.operation == 'DELETE' ? oldObject.metadata.name : object.metadata.name) "
        "!= 'fs2-public-edge-node-authority'"
    ) in source
    assert (
        "request.resource.resource != 'validatingadmissionpolicybindings' || "
        "(request.operation == 'DELETE' ? oldObject.metadata.name : object.metadata.name) "
        "!= 'fs2-public-edge-node-authority'"
    ) in source
    assert "impersonation_review_sha256 != strrep" in source
    assert "public_edge_node_authority_approval_exact" in source


def test_preventive_boundary_reopens_and_semantically_checks_raw_authority_exports() -> None:
    verifier = (
        ROOT
        / "stages/foundation/scripts/verify-public-edge-node-eligibility.py"
    ).read_text(encoding="utf-8")
    for filename in (
        "public-edge-preventive-provider-iam-export.json",
        "public-edge-preventive-apiserver-enforcement-export.json",
        "public-edge-preventive-identity-path-review.json",
        "public-edge-preventive-certificate-authority-history.json",
    ):
        assert filename in verifier
    assert "provider-IAM raw policy does not enforce exact-controller default deny" in verifier
    assert "API-server raw export does not enforce the protected exact-controller boundary" in verifier
    assert "raw RBAC/impersonation evidence does not deny every non-controller identity path" in verifier
    assert 'policy_spec["default_effect"] != "DENY"' in verifier
    assert 'admission["failure_policy"] != "Fail"' in verifier
    assert "unauthorized_credential_paths" in verifier
    assert "any(unauthorized_credential_paths.values())" in verifier
    assert "active_certificate_identities" in verifier
    assert "openssl_verify_certificate_chain(" in verifier
    assert "summaries do not bind the reopened raw exports" in verifier
    assert "review digests do not derive from reopened exports" in verifier


def preventive_native_export_fixture(
    *, role: str, endpoint: str, payload: dict[str, object]
) -> tuple[bytes, list[dict[str, object]]]:
    public_key = TEST_ED25519_PUBLIC_KEY
    key_id = "sha256:" + hashlib.sha256(public_key).hexdigest()
    authority = {
        "id": role.removesuffix("-attestor") + "-test",
        "key_id": key_id,
        "role": role,
    }
    envelope: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/native-authority-export/v1",
        "authority": authority,
        "payload": payload,
        "payload_sha256": GATE.canonical_sha256(payload),
    }
    envelope["signature"] = base64.urlsafe_b64encode(
        sign_ed25519(GATE.canonical_bytes(envelope))
    ).rstrip(b"=").decode("ascii")
    authorities = [
        {
            **authority,
            "endpoint": endpoint,
            "public_key": base64.urlsafe_b64encode(public_key)
            .rstrip(b"=")
            .decode("ascii"),
            "collector_executable_sha256": "1" * 64,
            "collector_config_sha256": "2" * 64,
            "runtime_review_sha256": "3" * 64,
        }
    ]
    return GATE.canonical_bytes(envelope) + b"\n", authorities


def native_list_fixture(
    *,
    api_group: str,
    resource: str,
    items: list[dict[str, object]],
    resource_version: str,
    partial_metadata: bool = False,
) -> dict[str, object]:
    request: dict[str, object] = {
        "api_group": api_group,
        "resource": resource,
        "scope": "all",
        "limit": 500,
        "continue": "",
    }
    if partial_metadata:
        request["accept"] = (
            "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
        )
    return {
        "api_group": api_group,
        "resource": resource,
        "scope": "all",
        "item_count": len(items),
        "page_count": 1,
        "pages": [
            {
                "request": request,
                "request_id": f"request-{resource}-0001",
                "response": {
                    "apiVersion": (
                        "meta.k8s.io/v1"
                        if partial_metadata
                        else ("v1" if not api_group else f"{api_group}/v1")
                    ),
                    "kind": (
                        "PartialObjectMetadataList"
                        if partial_metadata
                        else f"{resource.title()}List"
                    ),
                    "metadata": {
                        "continue": "",
                        "remainingItemCount": 0,
                        "resourceVersion": resource_version,
                    },
                    "items": items,
                },
            }
        ],
    }


def native_history_fixture(
    *, cluster_id: str, history_start: str, observed_through: str
) -> dict[str, object]:
    return {
        "history_start": history_start,
        "observed_through": observed_through,
        "page_count": 1,
        "record_count": 0,
        "pages": [
            {
                "request": {
                    "cluster_id": cluster_id,
                    "cursor": "",
                    "history_start": history_start,
                    "limit": 500,
                    "observed_through": observed_through,
                },
                "request_id": "request-ca-history-0001",
                "response": {
                    "next_cursor": "",
                    "records": [],
                    "remaining_count": 0,
                },
                "response_attestation_sha256": "9" * 64,
            }
        ],
    }


def minimal_protected_resource_contract() -> list[dict[str, object]]:
    namespace = "security"
    fixed: list[dict[str, object]] = [
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["validatingadmissionpolicies"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": [
                "fs2-public-edge-cas-bootstrap",
                "fs2-public-edge-node-authority",
                "fs2-public-edge-node-authority-cas",
            ],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["validatingadmissionpolicybindings"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": [
                "fs2-public-edge-cas-bootstrap-binding",
                "fs2-public-edge-node-authority-binding",
                "fs2-public-edge-node-authority-cas-binding",
            ],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "apiextensions.k8s.io",
            "api_version": "v1",
            "resources": ["customresourcedefinitions"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": ["publicedgenodeauthorityapprovals.security.fs2.nebius.ai"],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "security.fs2.nebius.ai",
            "api_version": "v1",
            "resources": ["publicedgenodeauthorityapprovals"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "exact-name-immutable",
            "names": ["fs2-public-edge-node-authority-approval"],
        },
        {
            "actions": ["bind", "create", "delete", "deletecollection", "escalate", "patch", "update"],
            "api_group": "rbac.authorization.k8s.io",
            "api_version": "v1",
            "resources": ["clusterrolebindings", "clusterroles", "rolebindings", "roles"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "deny-non-enrolled-authority-path",
            "names": ["*"],
        },
        {
            "actions": ["approve", "create", "delete", "deletecollection", "get", "list", "patch", "sign", "update", "watch"],
            "api_group": "certificates.k8s.io",
            "api_version": "v1",
            "resources": ["certificatesigningrequests", "certificatesigningrequests/approval"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-non-enrolled-certificate-path",
            "names": ["*"],
        },
        {
            "actions": ["create", "delete", "deletecollection", "patch", "update"],
            "api_group": "admissionregistration.k8s.io",
            "api_version": "v1",
            "resources": ["mutatingwebhookconfigurations", "validatingwebhookconfigurations"],
            "operations": ["CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-authority-intersecting-webhook-change",
            "names": ["*"],
        },
        {
            "actions": ["connect", "get", "list", "patch", "update", "watch"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["nodes", "nodes/proxy"],
            "operations": ["CONNECT", "CREATE", "DELETE", "UPDATE"],
            "namespaces": [],
            "semantic_guard": "deny-controller-credential-path",
            "names": ["*"],
        },
    ]
    scoped = [
        {
            "actions": ["delete", "deletecollection", "get", "list", "patch", "update", "watch"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["serviceaccounts"],
            "operations": ["DELETE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "deny-controller-service-account-path",
            "names": ["public-edge-authority"],
        },
        {
            "actions": ["create", "get"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["serviceaccounts/token"],
            "operations": ["CREATE"],
            "namespaces": [namespace],
            "semantic_guard": "deny-controller-tokenrequest-path",
            "names": ["public-edge-authority"],
        },
        {
            "actions": ["create", "patch", "update"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["pods", "replicationcontrollers", "serviceaccounts"],
            "operations": ["CREATE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "inspect-new-controller-credential-reachability",
            "names": ["*"],
        },
        {
            "actions": ["create", "patch", "update"],
            "api_group": "",
            "api_version": "v1",
            "resources": ["secrets"],
            "operations": ["CREATE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "classify-secret-content-before-admission",
            "names": ["*"],
        },
        {
            "actions": ["create", "patch", "update"],
            "api_group": "apps",
            "api_version": "v1",
            "resources": ["daemonsets", "deployments", "replicasets", "statefulsets"],
            "operations": ["CREATE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "inspect-new-controller-credential-reachability",
            "names": ["*"],
        },
        {
            "actions": ["create", "patch", "update"],
            "api_group": "batch",
            "api_version": "v1",
            "resources": ["cronjobs", "jobs"],
            "operations": ["CREATE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "inspect-new-controller-credential-reachability",
            "names": ["*"],
        },
        {
            "actions": ["delete", "deletecollection", "patch", "update"],
            "api_group": "apps",
            "api_version": "v1",
            "resources": ["deployments"],
            "operations": ["DELETE", "UPDATE"],
            "namespaces": [namespace],
            "semantic_guard": "deny-controller-credential-workload-path",
            "names": ["public-edge-authority"],
        },
    ]
    return [*fixed, *scoped]


def preventive_semantic_fixture(
    *, include_unenrolled_rbac_subject: bool = False
) -> dict[str, object]:
    collected_at = "2026-09-17T12:00:00Z"
    history_start = "2026-09-17T10:00:00Z"
    cluster_created_at = "2026-09-17T09:00:00Z"
    snapshot_id = "a" * 64
    image_digest = f"sha256:{'b' * 64}"
    provider_principal = "serviceaccount-controller123"
    controller_subject = {
        "kind": "ServiceAccount",
        "name": "public-edge-authority",
        "namespace": "security",
    }
    controller_username = "system:serviceaccount:security:public-edge-authority"
    controller_groups = [
        "system:authenticated",
        "system:serviceaccounts",
        "system:serviceaccounts:security",
    ]
    pod_template = {
        "serviceAccountName": "public-edge-authority",
        "initContainers": [],
        "containers": [
            {
                "name": "authority",
                "image": f"registry.invalid/public-edge-authority@{image_digest}",
            }
        ],
        "ephemeralContainers": [],
        "volumes": [],
    }
    deployment_uid = "00000000-0000-4000-8000-000000000301"
    controller_workload = {
        "api_version": "apps/v1",
        "images": [f"registry.invalid/public-edge-authority@{image_digest}"],
        "kind": "Deployment",
        "name": "public-edge-authority",
        "namespace": "security",
        "pod_template_sha256": GATE.canonical_sha256(pod_template),
        "service_account_name": "public-edge-authority",
        "uid": deployment_uid,
    }
    credential_workload = {
        "api_version": "apps/v1",
        "authority_secret_names": [],
        "kind": "Deployment",
        "name": "public-edge-authority",
        "namespace": "security",
        "pod_template_sha256": GATE.canonical_sha256(pod_template),
        "service_account_name": "public-edge-authority",
        "uid": deployment_uid,
    }
    ca_der = deterministic_test_ca_der()
    ca_pem = GATE.pem_encode_der(ca_der, "CERTIFICATE")
    ca_key_id = "sha256:" + GATE.openssl_public_key_sha256(
        ca_der, command="x509", input_format="DER"
    )
    ca_authority = {
        "ca_key_id": ca_key_id,
        "issuance_log_id": "issuance-log-test-001",
        "response_attestation_sha256": "8" * 64,
        "revocation_mode": "certificate-revocation-list",
        "signer_name": "fs2.test/client",
        "trust_anchor_key_ids": [ca_key_id],
        "trust_bundle_pem_base64": base64.b64encode(ca_pem).decode("ascii"),
        "trust_bundle_sha256": hashlib.sha256(ca_pem).hexdigest(),
    }
    enrollment = {
        "authority_id": "public-edge-controller-test",
        "capabilities": [
            "admission-authority-mutation",
            "protected-policy-mutation",
        ],
        "expires_at": "2026-09-17T13:00:00Z",
        "provider_principal_id": provider_principal,
        "subject": controller_subject,
    }
    identity_paths = [
        "anonymous",
        "authentication-webhook",
        "bootstrap-token",
        "client-certificate",
        "csr-approval",
        "csr-signing",
        "direct-user",
        "impersonated-group",
        "impersonated-uid",
        "impersonated-user",
        "impersonated-userextra",
        "kubelet-client-certificate",
        "node-credential",
        "oidc",
        "provider-control-plane",
        "requestheader-front-proxy",
        "service-account-token",
        "static-token",
    ]
    boundary = {
        "kind": "provider-iam+apiserver-admission",
        "provider_iam_policy_id": "provider-policy-test-001",
        "apiserver_enforcement_id": "apiserver-enforcement-test-001",
        "authority_snapshot_id": snapshot_id,
        "controller_username": controller_username,
        "controller_uid": "00000000-0000-4000-8000-000000000099",
        "controller_groups": controller_groups,
        "controller_allowed_image_digests": [image_digest],
        "controller_image_digest": image_digest,
        "controller_provider_principal_id": provider_principal,
        "certificate_history_start": history_start,
        "cluster_created_at": cluster_created_at,
        "credential_namespaces": ["security"],
        "enrolled_certificate_authorities": [ca_authority],
        "enrolled_certificate_identities": [],
        "enrolled_admission_webhooks": [],
        "enrolled_controller_workloads": [controller_workload],
        "enrolled_credential_secrets": [],
        "enrolled_credential_workloads": [credential_workload],
        "enrolled_identities": [enrollment],
        "identity_paths": identity_paths,
        "configuration_sha256": "4" * 64,
        "provenance_attestation_sha256": "5" * 64,
        "receipt_sha256": "6" * 64,
        "source_repository": "https://example.invalid/fs2",
        "source_commit": "7" * 40,
        "source_tree": "8" * 40,
    }
    protected_contract = minimal_protected_resource_contract()
    protected_names = sorted(
        {
            str(name)
            for contract in protected_contract
            for name in contract["names"]
        }
    )
    protected_actions = sorted(
        {
            str(action)
            for contract in protected_contract
            for action in contract["actions"]
        }
    )
    expected_controller = {
        "username": controller_username,
        "uid": boundary["controller_uid"],
        "groups": controller_groups,
        "image_digest": image_digest,
        "provider_principal_id": provider_principal,
    }
    provider_binding = {
        "apiVersion": "iam.nebius.ai/v1",
        "kind": "AccessBinding",
        "metadata": {
            "name": "public-edge-controller",
            "uid": "00000000-0000-4000-8000-000000000101",
            "resourceVersion": "provider-binding-rv-1",
        },
        "spec": {
            "effect": "ALLOW",
            "subject": {"type": "serviceAccount", "id": provider_principal},
            "actions": protected_actions,
            "resourceNames": protected_names,
            "protectedResources": protected_contract,
            "condition": {
                "project_id": "project-test123",
                "cluster_id": CLUSTER_ID,
                "configuration_sha256": boundary["configuration_sha256"],
                "authority_snapshot_id": snapshot_id,
            },
        },
    }
    provider_binding_list = native_list_fixture(
        api_group="iam.nebius.ai",
        resource="accessbindings",
        items=[provider_binding],
        resource_version="provider-bindings-rv-1",
    )
    provider_payload = {
        "schema": "fs2-serve.nebius.ai/public-edge-provider-iam-native-export/v3",
        "authority_snapshot_id": snapshot_id,
        "collected_at": collected_at,
        "provider_api": "nebius-iam/v1",
        "project_id": "project-test123",
        "cluster_id": CLUSTER_ID,
        "policy_id": boundary["provider_iam_policy_id"],
        "policy_get": {
            "request": {
                "operation": "get",
                "policy_id": boundary["provider_iam_policy_id"],
                "project_id": "project-test123",
            },
            "response": {
                "metadata": {
                    "id": boundary["provider_iam_policy_id"],
                    "parent_id": "project-test123",
                    "resource_version": "provider-policy-rv-1",
                },
                "spec": {
                    "default_effect": "DENY",
                    "protected_actions": protected_actions,
                    "protected_resource_names": protected_names,
                    "protected_resources": protected_contract,
                },
            },
            "request_id": "request-provider-policy-0001",
        },
        "access_binding_list": provider_binding_list,
    }
    ca_payload = {
        "schema": "fs2-serve.nebius.ai/public-edge-certificate-authority-history/v1",
        "authority_snapshot_id": snapshot_id,
        "cluster_id": CLUSTER_ID,
        "cluster_created_at": cluster_created_at,
        "collected_at": collected_at,
        "issuer_authorities": [ca_authority],
        "issuance_history": native_history_fixture(
            cluster_id=CLUSTER_ID,
            history_start=history_start,
            observed_through=collected_at,
        ),
        "revocation_history": native_history_fixture(
            cluster_id=CLUSTER_ID,
            history_start=history_start,
            observed_through=collected_at,
        ),
    }
    authentication = {
        "anonymous": False,
        "authentication_webhooks": [],
        "bootstrap_tokens": [],
        "client_certificate": {
            "configuration_sha256": "a" * 64,
            "enabled": True,
            "issuer_inventory_sha256": GATE.canonical_sha256([ca_authority]),
            "maximum_status_age_seconds": 300,
            "revocation_fail_closed": True,
            "revocation_inventory_sha256": GATE.canonical_sha256([]),
            "revocation_mode": "certificate-revocation-list",
        },
        "oidc_issuers": [],
        "provider_control_plane": {
            "enabled": True,
            "configuration_sha256": "b" * 64,
        },
        "requestheader": {
            "enabled": True,
            "configuration_sha256": "c" * 64,
        },
        "service_accounts": {
            "enabled": True,
            "configuration_sha256": "d" * 64,
        },
        "static_tokens": [],
    }
    authorization = {"modes": ["Node", "RBAC"], "webhooks": []}
    apiserver_payload = {
        "schema": "fs2-serve.nebius.ai/public-edge-apiserver-native-export/v4",
        "authority_snapshot_id": snapshot_id,
        "collected_at": collected_at,
        "cluster_id": CLUSTER_ID,
        "enforcement_id": boundary["apiserver_enforcement_id"],
        "resource_version": "apiserver-rv-1",
        "configuration_sha256": boundary["configuration_sha256"],
        "authentication_configuration": authentication,
        "authorization_configuration": authorization,
        "admission_configuration": {
            "failure_policy": "Fail",
            "match_policy": "Equivalent",
            "protected_resources": protected_contract,
            "default_decision": "Deny",
            "allowed_controller": expected_controller,
            "plugin": "ExternalPreventiveBoundary",
            "snapshot_fence": {
                "failure_policy": "Fail",
                "maximum_age_seconds": 300,
                "protected_resources_sha256": GATE.canonical_sha256(
                    protected_contract
                ),
                "snapshot_id": snapshot_id,
            },
        },
    }
    protected_policy_names = [
        "fs2-public-edge-cas-bootstrap",
        "fs2-public-edge-node-authority",
        "fs2-public-edge-node-authority-cas",
        "fs2-public-edge-cas-bootstrap-binding",
        "fs2-public-edge-node-authority-binding",
        "fs2-public-edge-node-authority-cas-binding",
    ]
    cluster_role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {
            "name": "public-edge-authority",
            "uid": "00000000-0000-4000-8000-000000000201",
            "resourceVersion": "clusterrole-rv-1",
        },
        "rules": [
            {
                "apiGroups": ["admissionregistration.k8s.io"],
                "resources": [
                    "validatingadmissionpolicies",
                    "validatingadmissionpolicybindings",
                ],
                "resourceNames": protected_policy_names,
                "verbs": ["create", "delete", "deletecollection", "patch", "update"],
            }
        ],
    }
    subjects: list[dict[str, object]] = [controller_subject]
    if include_unenrolled_rbac_subject:
        subjects.append(
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "User",
                "name": "unenrolled-platform-admin",
            }
        )
    cluster_binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": "public-edge-authority",
            "uid": "00000000-0000-4000-8000-000000000202",
            "resourceVersion": "clusterrolebinding-rv-1",
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "public-edge-authority",
        },
        "subjects": subjects,
    }
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "public-edge-authority",
            "namespace": "security",
            "uid": "00000000-0000-4000-8000-000000000203",
            "resourceVersion": "serviceaccount-rv-1",
        },
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "public-edge-authority",
            "namespace": "security",
            "uid": deployment_uid,
            "resourceVersion": "deployment-rv-1",
        },
        "spec": {"template": {"spec": pod_template}},
    }
    approval_crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {
            "name": "publicedgenodeauthorityapprovals.security.fs2.nebius.ai",
            "uid": "00000000-0000-4000-8000-000000000204",
            "resourceVersion": "approval-crd-rv-1",
        },
        "spec": {
            "group": "security.fs2.nebius.ai",
            "scope": "Cluster",
            "names": {
                "kind": "PublicEdgeNodeAuthorityApproval",
                "plural": "publicedgenodeauthorityapprovals",
            },
            "versions": [{"name": "v1", "served": True, "storage": True}],
            "conversion": {"strategy": "None"},
        },
    }
    approval_object = {
        "apiVersion": "security.fs2.nebius.ai/v1",
        "kind": "PublicEdgeNodeAuthorityApproval",
        "metadata": {
            "name": "fs2-public-edge-node-authority-approval",
            "uid": "00000000-0000-4000-8000-000000000205",
            "resourceVersion": "approval-rv-1",
        },
        "spec": {"preventiveBoundary": boundary},
        "status": {"accepted": True},
    }
    approval_projection = {
        "apiVersion": approval_object["apiVersion"],
        "kind": approval_object["kind"],
        "metadata": {
            "name": approval_object["metadata"]["name"],
            "resourceVersion": approval_object["metadata"]["resourceVersion"],
            "uid": approval_object["metadata"]["uid"],
        },
        "spec": approval_object["spec"],
        "status": approval_object["status"],
    }

    list_specs = {
        "cluster_roles": (
            "rbac.authorization.k8s.io",
            "clusterroles",
            [cluster_role],
            False,
        ),
        "cluster_role_bindings": (
            "rbac.authorization.k8s.io",
            "clusterrolebindings",
            [cluster_binding],
            False,
        ),
        "roles": ("rbac.authorization.k8s.io", "roles", [], False),
        "role_bindings": (
            "rbac.authorization.k8s.io",
            "rolebindings",
            [],
            False,
        ),
        "certificate_signing_requests": (
            "certificates.k8s.io",
            "certificatesigningrequests",
            [],
            False,
        ),
        "service_accounts": ("", "serviceaccounts", [service_account], False),
        "secret_metadata": ("", "secrets", [], True),
        "pods": ("", "pods", [], False),
        "replica_sets": ("apps", "replicasets", [], False),
        "replication_controllers": ("", "replicationcontrollers", [], False),
        "deployments": ("apps", "deployments", [deployment], False),
        "stateful_sets": ("apps", "statefulsets", [], False),
        "daemon_sets": ("apps", "daemonsets", [], False),
        "jobs": ("batch", "jobs", [], False),
        "cron_jobs": ("batch", "cronjobs", [], False),
        "validating_webhook_configurations": (
            "admissionregistration.k8s.io",
            "validatingwebhookconfigurations",
            [],
            False,
        ),
        "mutating_webhook_configurations": (
            "admissionregistration.k8s.io",
            "mutatingwebhookconfigurations",
            [],
            False,
        ),
        "custom_resource_definitions": (
            "apiextensions.k8s.io",
            "customresourcedefinitions",
            [approval_crd],
            False,
        ),
        "public_edge_node_authority_approvals": (
            "security.fs2.nebius.ai",
            "publicedgenodeauthorityapprovals",
            [approval_object],
            False,
        ),
    }
    native_lists = {
        name: native_list_fixture(
            api_group=api_group,
            resource=resource,
            items=items,
            resource_version=f"{resource}-list-rv-1",
            partial_metadata=partial,
        )
        for name, (api_group, resource, items, partial) in list_specs.items()
    }
    identity_payload = {
        "schema": "fs2-serve.nebius.ai/public-edge-kubernetes-authority-native-export/v4",
        "authority_snapshot_id": snapshot_id,
        "collected_at": collected_at,
        "project_id": "project-test123",
        "cluster_id": CLUSTER_ID,
        **native_lists,
        "secret_authority_classifications": [],
    }
    normalized_subjects = [controller_subject]
    if include_unenrolled_rbac_subject:
        normalized_subjects.append(
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "User",
                "name": "unenrolled-platform-admin",
            }
        )
    credential_path_subjects = {
        "admission-authority-mutation": normalized_subjects,
        "controller-secret-read": [],
        "controller-serviceaccount-mutation": [],
        "controller-workload-mutation": [],
        "csr-authority": [],
        "impersonation": [],
        "node-or-kubelet-proxy": [],
        "pod-subresource-access": [],
        "rbac-delegation": [],
        "serviceaccount-token-mint": [],
    }
    resource_versions = {
        "cluster_roles": "clusterroles-list-rv-1",
        "cluster_role_bindings": "clusterrolebindings-list-rv-1",
        "approval_objects": "publicedgenodeauthorityapprovals-list-rv-1",
        "cron_jobs": "cronjobs-list-rv-1",
        "custom_resource_definitions": "customresourcedefinitions-list-rv-1",
        "daemon_sets": "daemonsets-list-rv-1",
        "deployments": "deployments-list-rv-1",
        "jobs": "jobs-list-rv-1",
        "mutating_webhook_configurations": "mutatingwebhookconfigurations-list-rv-1",
        "pods": "pods-list-rv-1",
        "replica_sets": "replicasets-list-rv-1",
        "replication_controllers": "replicationcontrollers-list-rv-1",
        "roles": "roles-list-rv-1",
        "role_bindings": "rolebindings-list-rv-1",
        "secret_metadata": "secrets-list-rv-1",
        "service_accounts": "serviceaccounts-list-rv-1",
        "stateful_sets": "statefulsets-list-rv-1",
        "validating_webhook_configurations": "validatingwebhookconfigurations-list-rv-1",
    }
    rbac_projection = {
        "approval_objects": [approval_object],
        "authority_reachable_workloads": [credential_workload],
        "authority_secret_records": [],
        "cluster_roles": [cluster_role],
        "cluster_role_bindings": [cluster_binding],
        "credential_path_subjects": credential_path_subjects,
        "controller_workloads": [controller_workload],
        "controller_secret_metadata": [],
        "custom_resource_definitions": [approval_crd],
        "dangerous_mutating_webhooks": [],
        "dangerous_validating_webhooks": [],
        "daemon_sets": [],
        "deployments": [deployment],
        "enrolled_identities": [enrollment],
        "jobs": [],
        "cron_jobs": [],
        "mutating_webhook_configurations": [],
        "pods": [],
        "replica_sets": [],
        "replication_controllers": [],
        "roles": [],
        "role_bindings": [],
        "secret_metadata": [],
        "secret_authority_classifications": [],
        "service_accounts": [service_account],
        "stateful_sets": [],
        "validating_webhook_configurations": [],
        "resource_versions": resource_versions,
    }
    impersonation_projection = {
        "impersonating_subjects": [],
        "csr_authorities": [],
        "certificate_signing_requests": [],
        "csr_resource_version": "certificatesigningrequests-list-rv-1",
        "authentication_configuration": authentication,
        "authorization_configuration": authorization,
    }

    provider_raw, provider_authorities = preventive_native_export_fixture(
        role="provider-iam-native-response-attestor",
        endpoint="api.nebius.cloud",
        payload=provider_payload,
    )
    apiserver_raw, apiserver_authorities = preventive_native_export_fixture(
        role="kubernetes-apiserver-native-response-attestor",
        endpoint=f"kubernetes://{CLUSTER_ID}/configuration",
        payload=apiserver_payload,
    )
    identity_raw, identity_authorities = preventive_native_export_fixture(
        role="kubernetes-rbac-native-response-attestor",
        endpoint=f"kubernetes://{CLUSTER_ID}/rbac-csr",
        payload=identity_payload,
    )
    ca_history_raw, ca_authorities = preventive_native_export_fixture(
        role="kubernetes-ca-native-response-attestor",
        endpoint=f"kubernetes://{CLUSTER_ID}/certificate-authority-history",
        payload=ca_payload,
    )
    return {
        "provider_raw": provider_raw,
        "apiserver_raw": apiserver_raw,
        "identity_raw": identity_raw,
        "ca_history_raw": ca_history_raw,
        "provider_summary": {
            "provider_api": "nebius-iam/v1",
            "resource_version": "provider-policy-rv-1",
            "binding_resource_version": "provider-bindings-rv-1",
        },
        "apiserver_summary": {"resource_version": "apiserver-rv-1"},
        "identity_summary": {
            "rbac_review_sha256": GATE.canonical_sha256(rbac_projection),
            "impersonation_review_sha256": GATE.canonical_sha256(
                impersonation_projection
            ),
        },
        "ca_history_summary": {
            "cluster_id": CLUSTER_ID,
            "authority_snapshot_id": snapshot_id,
            "cluster_created_at": cluster_created_at,
            "collected_at": collected_at,
            "history_start": history_start,
            "issuance_count": 0,
            "revocation_count": 0,
            "raw_export_sha256": hashlib.sha256(ca_history_raw).hexdigest(),
        },
        "boundary": boundary,
        "approval_projection": approval_projection,
        "collected_at": datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        "response_authorities": [
            *provider_authorities,
            *apiserver_authorities,
            *identity_authorities,
            *ca_authorities,
        ],
    }


def test_preventive_boundary_authenticates_all_four_native_exports() -> None:
    fixtures = (
        (
            GATE.PREVENTIVE_PROVIDER_IAM_EXPORT_FILENAME,
            "provider-iam-native-response-attestor",
            "api.nebius.cloud",
        ),
        (
            GATE.PREVENTIVE_APISERVER_EXPORT_FILENAME,
            "kubernetes-apiserver-native-response-attestor",
            f"kubernetes://{CLUSTER_ID}/configuration",
        ),
        (
            GATE.PREVENTIVE_IDENTITY_REVIEW_FILENAME,
            "kubernetes-rbac-native-response-attestor",
            f"kubernetes://{CLUSTER_ID}/rbac-csr",
        ),
        (
            GATE.PREVENTIVE_CA_HISTORY_FILENAME,
            "kubernetes-ca-native-response-attestor",
            f"kubernetes://{CLUSTER_ID}/certificate-authority-history",
        ),
    )
    for index, (filename, role, endpoint) in enumerate(fixtures):
        payload = {
            "authority_snapshot_id": "a" * 64,
            "fixture_index": index,
            "schema": f"fs2-serve.nebius.ai/test-native-payload/v{index + 1}",
        }
        raw, authorities = preventive_native_export_fixture(
            role=role, endpoint=endpoint, payload=payload
        )
        assert GATE.verify_native_authority_export(
            raw,
            filename,
            response_authorities=authorities,
            role=role,
            endpoint=endpoint,
        ) == payload

    verifier = SCRIPT.read_text(encoding="utf-8")
    for required_argument in (
        "ca_history_raw: bytes",
        "ca_history_summary: Mapping[str, Any]",
        "approval_projection: Mapping[str, Any]",
        "response_authorities: Sequence[object]",
    ):
        assert required_argument in verifier


def test_preventive_boundary_rejects_tampered_native_identity_export(
) -> None:
    role = "kubernetes-rbac-native-response-attestor"
    endpoint = f"kubernetes://{CLUSTER_ID}/rbac-csr"
    raw, authorities = preventive_native_export_fixture(
        role=role,
        endpoint=endpoint,
        payload={"schema": "fs2-serve.nebius.ai/test-identity/v1", "checks": []},
    )
    envelope = json.loads(raw)
    envelope["payload"]["checks"] = [{"path": "forged-rbac-path"}]
    tampered = GATE.canonical_bytes(envelope) + b"\n"
    with pytest.raises(GATE.GateError, match="payload digest differs"):
        GATE.verify_native_authority_export(
            tampered,
            GATE.PREVENTIVE_IDENTITY_REVIEW_FILENAME,
            response_authorities=authorities,
            role=role,
            endpoint=endpoint,
        )


def test_preventive_boundary_raw_exports_close_every_identity_path() -> None:
    fixture = preventive_semantic_fixture()
    GATE.validate_preventive_raw_exports(
        provider_raw=fixture["provider_raw"],
        apiserver_raw=fixture["apiserver_raw"],
        identity_raw=fixture["identity_raw"],
        ca_history_raw=fixture["ca_history_raw"],
        provider_summary=fixture["provider_summary"],
        apiserver_summary=fixture["apiserver_summary"],
        identity_summary=fixture["identity_summary"],
        ca_history_summary=fixture["ca_history_summary"],
        boundary=fixture["boundary"],
        approval_projection=fixture["approval_projection"],
        project_id="project-test123",
        cluster_id=CLUSTER_ID,
        collected_at=fixture["collected_at"],
        response_authorities=fixture["response_authorities"],
    )


def test_preventive_boundary_rejects_one_rbac_allowed_identity_path() -> None:
    fixture = preventive_semantic_fixture(include_unenrolled_rbac_subject=True)
    with pytest.raises(GATE.GateError, match="does not deny every"):
        GATE.validate_preventive_raw_exports(
            provider_raw=fixture["provider_raw"],
            apiserver_raw=fixture["apiserver_raw"],
            identity_raw=fixture["identity_raw"],
            ca_history_raw=fixture["ca_history_raw"],
            provider_summary=fixture["provider_summary"],
            apiserver_summary=fixture["apiserver_summary"],
            identity_summary=fixture["identity_summary"],
            ca_history_summary=fixture["ca_history_summary"],
            boundary=fixture["boundary"],
            approval_projection=fixture["approval_projection"],
            project_id="project-test123",
            cluster_id=CLUSTER_ID,
            collected_at=fixture["collected_at"],
            response_authorities=fixture["response_authorities"],
        )


def test_empty_source_issuer_registry_fails_closed() -> None:
    receipt, _trust, adapter_trust, evidence_raw = membership_receipt()
    with pytest.raises(GATE.GateError, match="source-trusted authority"):
        GATE.validate_membership_receipt(
            receipt,
            {"schema": GATE.MEMBERSHIP_TRUST_SCHEMA, "issuers": []},
            adapter_trust,
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_empty_provider_adapter_registry_fails_closed(monkeypatch) -> None:
    receipt, trust, _adapter_trust, evidence_raw = membership_receipt()
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    with pytest.raises(GATE.GateError, match="source-enrolled adapter authority"):
        GATE.validate_membership_receipt(
            receipt,
            trust,
            {"schema": GATE.PROVIDER_ADAPTER_TRUST_SCHEMA, "adapters": []},
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_identity_tuple_cannot_be_added_as_membership_authority(monkeypatch) -> None:
    receipt, trust, adapter_trust, evidence_raw = membership_receipt()
    receipt["payload"]["provider_membership"]["kubernetes_node_controller"] = {
        "username": "system:serviceaccount:forged:controller"
    }
    receipt["payload_sha256"] = hashlib.sha256(
        GATE.canonical_bytes(receipt["payload"])
    ).hexdigest()
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    monkeypatch.setattr(
        GATE,
        "checked_executable",
        lambda _record, name: sys.executable if name == "python3" else f"/usr/bin/{name}",
    )
    with pytest.raises(GATE.GateError, match="must contain exactly"):
        GATE.validate_membership_receipt(
            receipt,
            trust,
            adapter_trust,
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_signed_relation_cannot_be_replaced_by_a_name_derived_member(monkeypatch) -> None:
    receipt, trust, adapter_trust, evidence_raw = membership_receipt()
    receipt["payload"]["provider_membership"]["member_instance_ids"][0] = (
        "computeinstance-attacker"
    )
    receipt["payload"]["provider_membership"]["member_instance_ids"].sort()
    receipt["payload_sha256"] = hashlib.sha256(
        GATE.canonical_bytes(receipt["payload"])
    ).hexdigest()
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    monkeypatch.setattr(
        GATE,
        "checked_executable",
        lambda _record, name: sys.executable if name == "python3" else f"/usr/bin/{name}",
    )
    with pytest.raises(GATE.GateError, match="admission union must equal exact provider membership"):
        GATE.validate_membership_receipt(
            receipt,
            trust,
            adapter_trust,
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_checked_executable_uses_and_closes_the_pinned_descriptor(monkeypatch) -> None:
    read_descriptor, write_descriptor = os.pipe()
    os.close(write_descriptor)
    monkeypatch.setattr(
        GATE,
        "pin_executable",
        lambda *_args: ("/usr/bin/tool", "/usr/bin/tool", read_descriptor),
    )

    assert GATE.checked_executable({}, "provider CLI") == "/usr/bin/tool"
    with pytest.raises(OSError):
        os.fstat(read_descriptor)


def test_checked_kubeconfig_is_reused_only_through_sealed_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    kubeconfig = run_root / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    kubeconfig.chmod(0o600)
    monkeypatch.setattr(GATE, "validate_parent_chain", lambda *_args: None)

    resolved_root, pinned_path, descriptor, snapshot_sha256 = GATE.checked_local_inputs(
        str(run_root), str(kubeconfig)
    )
    try:
        assert resolved_root == run_root.resolve()
        assert pinned_path == f"/proc/self/fd/{descriptor}"
        assert snapshot_sha256 == hashlib.sha256(b"apiVersion: v1\n").hexdigest()
        with pytest.raises(OSError):
            os.write(descriptor, b"changed")
    finally:
        os.close(descriptor)


def test_provider_reads_receive_only_pinned_fds_and_sanitized_environment(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

    monkeypatch.setattr(GATE.subprocess, "run", fake_run)
    monkeypatch.setattr(
        GATE.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/exact-user")
    )
    monkeypatch.setattr(GATE, "PINNED_COMMAND_FDS", (41, 42, 43))

    assert GATE.run_json(["/proc/self/fd/42", "get"], "provider read") == {
        "ok": True
    }
    assert observed["command"] == ["/proc/self/fd/42", "get"]
    assert observed["pass_fds"] == (41, 42, 43)
    assert observed["close_fds"] is True
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["cwd"] == "/"
    assert observed["env"] == {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def test_complete_pagination_follows_every_provider_token(monkeypatch) -> None:
    pages = iter(
        (
            {"items": [provider_instance(1)], "next_page_token": "page-2"},
            {"items": [provider_instance(2), provider_instance(3)]},
        )
    )
    commands: list[list[str]] = []

    def fake_run_json(command: list[str], _label: str) -> dict[str, object]:
        commands.append(command)
        return next(pages)

    monkeypatch.setattr(GATE, "run_json", fake_run_json)
    result = GATE.paginated_list(
        ["nebius", "compute", "instance"],
        ["list", "--parent-id", "project-test123"],
        "Compute instances",
    )

    assert result["pagination"] == {
        "complete": True,
        "page_count": 2,
        "item_count": 3,
        "terminal_next_page_token": "",
    }
    assert "--page-token" not in commands[0]
    assert commands[1][-2:] == ["--page-token", "page-2"]


def test_pagination_rejects_a_repeated_continuation_token(monkeypatch) -> None:
    pages = iter(
        (
            {"items": [provider_instance(1)], "next_page_token": "repeat"},
            {"items": [provider_instance(2)], "next_page_token": "repeat"},
        )
    )
    monkeypatch.setattr(GATE, "run_json", lambda _command, _label: next(pages))
    with pytest.raises(GATE.GateError, match="repeated a pagination token"):
        GATE.paginated_list(
            ["nebius", "compute", "instance"],
            ["list", "--parent-id", "project-test123"],
            "Compute instances",
        )


def test_provider_list_and_exact_get_must_have_the_same_revision() -> None:
    changed_gets = provider_instance_gets()
    changed_gets["computeinstance-test1"] = provider_instance(1, "99")
    with pytest.raises(GATE.GateError, match="list and exact-ID get projections differ"):
        GATE.validate_provider_members(
            provider_instances(),
            changed_gets,
            provider_instances(),
            provider_instance_gets(),
            project_id="project-test123",
            signed_instance_ids=sorted(provider_member_set()),
            expected_count=3,
            maximum_surge=1,
        )


def test_provider_membership_rejects_kubernetes_only_foreign_node() -> None:
    foreign = node_list("201")
    foreign["items"][0]["metadata"]["name"] = "computeinstance-foreign"
    foreign["items"][0]["metadata"]["labels"][
        "kubernetes.io/hostname"
    ] = "computeinstance-foreign"
    foreign["items"][0]["spec"]["providerID"] = "nebius://computeinstance-foreign"
    with pytest.raises(GATE.GateError, match="absent from provider membership"):
        GATE.validate_nodes(
            node_list("200"),
            foreign,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            serving_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )


def test_saved_plan_older_than_prerequisite_compatible_window_is_rejected() -> None:
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(GATE.GateError, match="exceeding the 14400s"):
        GATE.parse_plan_timestamp(
            (now - timedelta(seconds=14401)).isoformat(),
            now=now,
            maximum_age=14400,
        )


def test_unsigned_name_convention_cannot_add_a_provider_member() -> None:
    extra = provider_instance(4)
    extra["metadata"]["name"] = f"{GROUP_ID}-abc12-def34"
    listed = provider_instances()
    listed["items"].append(extra)
    listed["pagination"]["item_count"] = 4

    assert GATE.provider_member_ids(
        listed,
        project_id="project-test123",
        signed_instance_ids=sorted(provider_member_set()),
    ) == sorted(provider_member_set())


def test_signed_member_absent_from_complete_provider_list_is_rejected() -> None:
    incomplete = provider_instances()
    incomplete["items"] = incomplete["items"][:-1]
    incomplete["pagination"]["item_count"] = 2
    with pytest.raises(GATE.GateError, match="absent from the complete Compute"):
        GATE.provider_member_ids(
            incomplete,
            project_id="project-test123",
            signed_instance_ids=sorted(provider_member_set()),
        )


def test_spoofed_scheduler_labels_without_provider_membership_are_rejected() -> None:
    after = node_list("201")
    after["items"][0]["metadata"]["annotations"][
        "cluster.x-k8s.io/owner-name"
    ] = "mk8snodegroup-foreign"
    with pytest.raises(GATE.GateError, match="corroborating Cluster API ownership"):
        GATE.validate_nodes(
            node_list("200"),
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            serving_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )


def test_provider_rollout_machineset_suffix_remains_bound_to_exact_group() -> None:
    before = node_list("200")
    for item in before["items"]:
        annotations = item["metadata"]["annotations"]
        annotations["cluster.x-k8s.io/owner-name"] = f"{GROUP_ID}-abc12"
        annotations["cluster.x-k8s.io/machine"] = f"{GROUP_ID}-abc12-def34"
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "201"

    _revision, count, domains, _digest = GATE.validate_nodes(
        before,
        after,
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        selector=SELECTOR,
        provider_member_ids=provider_member_set(),
        serving_member_ids=provider_member_set(),
        expected_count=3,
        minimum_domains=3,
    )
    assert count == domains == 3


def test_changed_provider_resource_version_is_rejected() -> None:
    before = node_group("11")
    after = node_group("12")
    with pytest.raises(GATE.GateError, match="changed during"):
        GATE.validate_node_group(
            complete_list([copy.deepcopy(before)]),
            complete_list([copy.deepcopy(after)]),
            before,
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            expected_count=3,
            maximum_surge=1,
            membership_phase="stable",
            selector=SELECTOR,
        )


def test_no_schedule_taint_removes_node_from_current_eligible_capacity() -> None:
    before = node_list("200")
    before["items"][0]["spec"]["taints"] = [
        {"key": "maintenance", "effect": "NoSchedule"}
    ]
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "201"
    with pytest.raises(GATE.GateError, match="fewer provider-owned Nodes"):
        GATE.validate_nodes(
            before,
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            serving_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )


def test_final_admission_contract_is_stable_and_matches_exact_source_hash() -> None:
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {
            "name": "fs2-public-edge-node-authority",
            "uid": "00000000-0000-4000-8000-000000000777",
            "resourceVersion": "700",
            "annotations": {"fs2.nebius.ai/membership-payload-sha256": "a" * 64},
        },
        "spec": {"failurePolicy": "Fail", "validations": [{"expression": "true"}]},
    }
    _revision, _uid, projection = GATE.admission_contract_projection(
        policy, kind="ValidatingAdmissionPolicy"
    )
    expected_sha256 = GATE.terraform_json_sha256(projection)
    revision, uid, observed_sha256 = GATE.validate_admission_contract(
        policy,
        copy.deepcopy(policy),
        kind="ValidatingAdmissionPolicy",
        expected_sha256=expected_sha256,
    )
    assert revision == "700"
    assert uid.endswith("777")
    assert observed_sha256 == expected_sha256

    changed = copy.deepcopy(policy)
    changed["spec"]["failurePolicy"] = "Ignore"
    with pytest.raises(GATE.GateError, match="changed during"):
        GATE.validate_admission_contract(
            policy,
            changed,
            kind="ValidatingAdmissionPolicy",
            expected_sha256=expected_sha256,
        )
