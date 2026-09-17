from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import struct
import sys
import uuid
from types import ModuleType
from typing import Any

import pytest
from conftest import CONTROL_ROOT
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = CONTROL_ROOT / "scripts" / "network_policy_security_enforcer.py"
APPROVAL_SCRIPT = CONTROL_ROOT / "scripts" / "network_policy_security_recovery_approval.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("network_policy_security_enforcer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ENFORCER = _load_module()


def _load_approval_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("network_policy_security_recovery_approval", APPROVAL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


APPROVAL = _load_approval_module()
CLUSTER = {"api_server_sha256": "a" * 64, "kube_system_uid": "kube-system-uid-000000000000"}
IDENTITY_EPOCH = "identity-epoch-001"
PRIOR_IDENTITY_EPOCH = "identity-epoch-000"
SUCCESSOR_IDENTITY_EPOCH = "identity-epoch-002"


def _principal(role: str, epoch: str) -> str:
    return f"fs2-np-{role}-{hashlib.sha256(epoch.encode()).hexdigest()[:16]}"


SECURITY_USER_INFO = {
    "username": _principal("security-owner", IDENTITY_EPOCH),
    "uid": "security-owner-uid",
    "groups": ["system:authenticated"],
    "extra": {},
}


def _public_value(private_key: Ed25519PrivateKey) -> str:
    return base64.urlsafe_b64encode(private_key.public_key().public_bytes_raw()).decode().rstrip("=")


def _metadata(name: str, namespace: str, uid: str, resource_version: int, role: str | None = None) -> dict[str, Any]:
    labels = {ENFORCER.BOUNDARY_LABEL: ENFORCER.BOUNDARY_VALUE}
    if role:
        labels[ENFORCER.ROLE_LABEL] = role
    return {
        "name": name,
        "namespace": namespace,
        "uid": uid,
        "resourceVersion": str(resource_version),
        "labels": labels,
    }


class FakeAPI:
    def __init__(self, objects: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        self.objects = copy.deepcopy(objects)
        self.patches: list[tuple[str, str, str, Any, bool]] = []
        self.fail_once: tuple[str, str, str] | None = None
        self.events: list[str] = []

    def cluster_identity(self) -> dict[str, str]:
        return dict(CLUSTER)

    def user_info(self) -> dict[str, Any]:
        return copy.deepcopy(SECURITY_USER_INFO)

    def get(self, resource: str, name: str, namespace: str = "") -> dict[str, Any]:
        return copy.deepcopy(self.objects[(resource, name, namespace)])

    def patch(
        self,
        resource: str,
        name: str,
        namespace: str,
        patch_type: str,
        patch: Any,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        key = (resource, name, namespace)
        self.events.append(f"patch:{resource}:{name}")
        if self.fail_once == key:
            self.fail_once = None
            raise ENFORCER.EnforcerError("injected Kubernetes failure")
        current = copy.deepcopy(self.objects[key])
        if patch_type == "merge":
            for section, value in patch.items():
                if isinstance(value, dict) and isinstance(current.get(section), dict):
                    current[section].update(copy.deepcopy(value))
                else:
                    current[section] = copy.deepcopy(value)
        else:
            for item in patch:
                path = item["path"].strip("/").split("/")
                cursor = current
                for part in path[:-1]:
                    cursor = cursor.setdefault(part, {})
                if item["op"] == "add":
                    cursor[path[-1]] = item["value"]
        current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
        self.patches.append((resource, name, namespace, copy.deepcopy(patch), dry_run))
        if not dry_run:
            self.objects[key] = copy.deepcopy(current)
        return current


def _fixture() -> tuple[
    FakeAPI,
    Any,
    Ed25519PrivateKey,
    Ed25519PrivateKey,
    Ed25519PrivateKey,
]:
    client_key = Ed25519PrivateKey.generate()
    server_key = Ed25519PrivateKey.generate()
    recovery_key = Ed25519PrivateKey.generate()
    client_value = _public_value(client_key)
    server_value = _public_value(server_key)
    recovery_value = _public_value(recovery_key)
    now = dt.datetime.now(dt.UTC)
    contract = {
        "schema": "fs2-serve.nebius.ai/network-policy-boundary-topology/v1",
        "mode": "public",
        "security_owner_username": _principal("security-owner", IDENTITY_EPOCH),
        "security_bootstrap_username": _principal("security-bootstrap", IDENTITY_EPOCH),
        "successor_security_owner_username": _principal("security-owner", SUCCESSOR_IDENTITY_EPOCH),
        "successor_security_bootstrap_username": _principal("security-bootstrap", SUCCESSOR_IDENTITY_EPOCH),
        "gateway_namespace": "envoy-gateway-system",
        "controller_namespace": "envoy-gateway-system",
        "policy_names": {
            "proxy_normal": "fs2-serve-control-plane-public-envoy",
            "proxy_guard": "fs2-serve-control-plane-public-envoy-transition-guard",
            "controller_normal": "fs2-serve-control-plane-envoy-controller-xds",
            "controller_guard": "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
            "default_deny": "fs2-serve-control-plane-envoy-default-deny",
        },
        "security_handoff": {
            "schema": "fs2-serve.nebius.ai/network-policy-security-handoff/v2",
            "client_public_key_sha256": ENFORCER.sha256_text(client_value),
            "server_public_key_sha256": ENFORCER.sha256_text(server_value),
            "recovery_public_key_sha256": ENFORCER.sha256_text(recovery_value),
            "peer_uid": 1001,
            "peer_gid": 1002,
            "peer_gid_contract": "effective-dedicated",
            "socket_path": "/run/fs2/identity-epoch-001/security.sock",
            "socket_directory_contract": "precreated-setgid-02710",
            "identity_boundary": {
                "schema": "fs2-serve.nebius.ai/network-policy-identity-boundary/v3",
                "identity_epoch": IDENTITY_EPOCH,
                "prior_identity_epoch": PRIOR_IDENTITY_EPOCH,
                "successor_identity_epoch": SUCCESSOR_IDENTITY_EPOCH,
                "epoch_principals": {
                    "release": _principal("release", IDENTITY_EPOCH),
                    "security_owner": _principal("security-owner", IDENTITY_EPOCH),
                    "security_bootstrap": _principal("security-bootstrap", IDENTITY_EPOCH),
                    "prior_owner": _principal("security-owner", PRIOR_IDENTITY_EPOCH),
                    "prior_bootstrap": _principal("security-bootstrap", PRIOR_IDENTITY_EPOCH),
                    "successor_owner": _principal("security-owner", SUCCESSOR_IDENTITY_EPOCH),
                    "successor_bootstrap": _principal("security-bootstrap", SUCCESSOR_IDENTITY_EPOCH),
                },
                "release_user_info_sha256": "b" * 64,
                "security_user_info_sha256": ENFORCER.sha256_json(SECURITY_USER_INFO),
                "bootstrap_user_info_sha256": "c" * 64,
                "prior_security_user_info_sha256": "f" * 64,
                "prior_bootstrap_user_info_sha256": "0" * 64,
                "credential_set_sha256": "a" * 64,
                "release_kubeconfig_sha256": "b" * 64,
                "security_kubeconfig_sha256": "c" * 64,
                "bootstrap_kubeconfig_sha256": "e" * 64,
                "prior_security_kubeconfig_sha256": "3" * 64,
                "prior_bootstrap_kubeconfig_sha256": "4" * 64,
                "security_subject_inventory_sha256": "d" * 64,
                "provider_subject_snapshot_sha256": "1" * 64,
                "provider_trust_anchor_sha256": "5" * 64,
                "provider_adapter_sha256": "6" * 64,
                "provider_execution_sha256": "a" * 64,
                "kubernetes_authentication_sha256": "b" * 64,
                "kubernetes_subject_inventory_sha256": "2" * 64,
                "effective_rbac_subjects_sha256": "4" * 64,
                "auditor_bootstrap_sha256": "7" * 64,
                "external_role_bundle_sha256": "9" * 64,
                "plan_rotation_phase": "preapply",
                "rotation_binding_state_sha256": "8" * 64,
                "plan_preflight_verified": True,
                "plan_preflight_sha256": "e" * 64,
                "release_expires_at": (now + dt.timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
                "security_expires_at": (now + dt.timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
                "bootstrap_expires_at": (now - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "rollback_valid_until": (now + dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "minimum_rollback_seconds": 3600,
                "rotation_contract": {
                    "mechanism": "preauthorized-successor-epoch",
                    "bootstrap_update_identity": _principal("security-bootstrap", IDENTITY_EPOCH),
                    "successor_security_owner_identity": _principal("security-owner", SUCCESSOR_IDENTITY_EPOCH),
                    "successor_bootstrap_identity": _principal("security-bootstrap", SUCCESSOR_IDENTITY_EPOCH),
                    "prior_security_owner_identity": _principal("security-owner", PRIOR_IDENTITY_EPOCH),
                    "prior_bootstrap_identity": _principal("security-bootstrap", PRIOR_IDENTITY_EPOCH),
                    "new_paths_required": True,
                    "allowed_plan_phases": ["preapply", "resume", "postapply"],
                    "mutation_binding_states": ["before", "target"],
                    "fresh_preapply_prior_owner_authorized": True,
                    "prior_bootstrap_protected_mutation_denied": True,
                    "postapply_retirement_required": True,
                },
                "bootstrap_must_be_expired": True,
                "permitted_shared_groups": ["system:authenticated", "system:serviceaccounts"],
            },
            "cluster": CLUSTER,
            "allowed_actions": ["transition-mutation", "set-admission-recovery"],
            "delete_allowed": False,
            "auditor_bootstrap": {
                "mechanism": "external-preprovision-declarative-import",
                "cluster_role": "fs2-network-policy-security-auditor",
                "cluster_role_binding": "fs2-network-policy-security-auditor",
                "bootstrap_cluster_role": "fs2-network-policy-security-bootstrap",
                "bootstrap_namespaced_roles": {
                    "state": "fs2-network-policy-transition-bootstrap",
                    "gateway": "fs2-network-policy-transition-gateway-bootstrap",
                    "controller": "fs2-network-policy-transition-controller-bootstrap",
                },
                "immutable_role_bundle_sha256": "9" * 64,
                "preapply_subjects": [
                    _principal("security-bootstrap", PRIOR_IDENTITY_EPOCH),
                    _principal("security-bootstrap", IDENTITY_EPOCH),
                ],
                "desired_subjects": [
                    _principal("security-bootstrap", IDENTITY_EPOCH),
                    _principal("security-bootstrap", SUCCESSOR_IDENTITY_EPOCH),
                ],
                "observed_sha256": "7" * 64,
                "bootstrap_create": False,
                "bootstrap_exact_update": True,
                "delete_allowed": False,
            },
        },
    }
    topology = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(ENFORCER.TOPOLOGY_NAME, ENFORCER.RELEASE_NAMESPACE, "topology-uid", 11),
        "data": {"topology.json": ENFORCER.canonical(contract)},
    }
    lease = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": _metadata(ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE, "lease-uid", 12),
        "spec": {
            "holderIdentity": "coordinator:operation",
            "leaseTransitions": 7,
            "leaseDurationSeconds": 60,
            "renewTime": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        },
    }
    proxy_spec = {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "envoy"}},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    }
    candidate = {
        "candidate_sha256": "c" * 64,
        "topology": {
            "contract": contract,
            "uid": "topology-uid",
            "resource_version": "11",
            "sha256": ENFORCER.sha256_json(contract),
        },
        "policies": {
            "public-envoy": {
                "normal_name": "fs2-serve-control-plane-public-envoy",
                "spec": proxy_spec,
            }
        },
    }
    receipt = {
        "schema": ENFORCER.RECEIPT_SCHEMA,
        "phase": "guards-staging",
        "candidate": candidate,
        "intent": {"operation": "stage-guards"},
    }
    receipt_object = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE, "receipt-uid", 13),
        "data": {"receipt.json": ENFORCER.canonical(receipt)},
    }
    proxy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(
            contract["policy_names"]["proxy_guard"],
            "envoy-gateway-system",
            "proxy-uid",
            14,
            "public-envoy",
        ),
        "spec": {"podSelector": {"matchLabels": {"uninitialized": "true"}}, "policyTypes": ["Ingress"]},
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": _metadata(ENFORCER.ADMISSION_NAME, "", "binding-uid", 15),
        "spec": {"policyName": ENFORCER.ADMISSION_NAME, "validationActions": ["Deny"]},
    }
    parameter = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(ENFORCER.PARAMETER_NAME, ENFORCER.RELEASE_NAMESPACE, "parameter-uid", 16),
        "data": {
            "schema": "fs2-serve.nebius.ai/network-policy-boundary-parameters/v1",
            "mode": "deny",
            "delete_allowed": "false",
            "signer_key_id": ENFORCER.sha256_text(server_value),
            "recovery_signer_key_id": ENFORCER.sha256_text(recovery_value),
        },
    }
    objects = {
        ("configmap", ENFORCER.TOPOLOGY_NAME, ENFORCER.RELEASE_NAMESPACE): topology,
        ("lease", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE): lease,
        ("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE): receipt_object,
        ("networkpolicy", contract["policy_names"]["proxy_guard"], "envoy-gateway-system"): proxy,
        ("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, ""): binding,
        ("configmap", ENFORCER.PARAMETER_NAME, ENFORCER.RELEASE_NAMESPACE): parameter,
    }
    api = FakeAPI(objects)
    enforcer = ENFORCER.SecurityEnforcer(
        api,
        client_public_key_value=client_value,
        server_private_key=server_key,
        recovery_public_key_value=recovery_value,
        expected_cluster=CLUSTER,
        expected_peer_uid=1001,
        expected_peer_gid=1002,
        expected_socket_path=ENFORCER.Path("/run/fs2/identity-epoch-001/security.sock"),
        security_kubeconfig_sha256="c" * 64,
    )
    return api, enforcer, client_key, server_key, recovery_key


def _signed_request(client_key: Ed25519PrivateKey, action: str, body: dict[str, Any]) -> dict[str, Any]:
    issued = dt.datetime.now(dt.UTC)
    client_value = _public_value(client_key)
    signed = {
        "schema": ENFORCER.REQUEST_SCHEMA,
        "operation_id": str(uuid.uuid4()),
        "client_key_id": ENFORCER.sha256_text(client_value),
        "action": action,
        "cluster": CLUSTER,
        "release": {"name": ENFORCER.RELEASE_NAME, "namespace": ENFORCER.RELEASE_NAMESPACE},
        "issued_at": issued.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "expires_at": (issued + dt.timedelta(seconds=20)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "body": body,
    }
    signature = base64.urlsafe_b64encode(client_key.sign(ENFORCER.canonical(signed).encode())).decode().rstrip("=")
    return {"signed": signed, "signature": signature}


def _topology_evidence(api: FakeAPI) -> dict[str, Any]:
    resource = api.get("configmap", ENFORCER.TOPOLOGY_NAME, ENFORCER.RELEASE_NAMESPACE)
    contract = json.loads(resource["data"]["topology.json"])
    return {
        "namespace": ENFORCER.RELEASE_NAMESPACE,
        "name": ENFORCER.TOPOLOGY_NAME,
        "uid": resource["metadata"]["uid"],
        "resource_version": resource["metadata"]["resourceVersion"],
        "sha256": ENFORCER.sha256_json(contract),
    }


def _transition_body(api: FakeAPI) -> dict[str, Any]:
    topology = json.loads(
        api.get("configmap", ENFORCER.TOPOLOGY_NAME, ENFORCER.RELEASE_NAMESPACE)["data"]["topology.json"]
    )
    name = topology["policy_names"]["proxy_guard"]
    target = api.get("networkpolicy", name, "envoy-gateway-system")
    lease = api.get("lease", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE)
    receipt_object = api.get("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE)
    receipt = ENFORCER.receipt_from_object(receipt_object)
    target_evidence = ENFORCER.object_evidence(target)
    lease_evidence = ENFORCER.object_evidence(lease)
    lease_evidence.update(
        holder_identity=lease["spec"]["holderIdentity"],
        lease_transitions=lease["spec"]["leaseTransitions"],
    )
    receipt_evidence = ENFORCER.object_evidence(receipt_object)
    receipt_evidence.update(
        receipt_sha256=ENFORCER.sha256_json(receipt),
        phase=receipt["phase"],
        intent=receipt["intent"],
    )
    patch = {
        "metadata": {
            "resourceVersion": target["metadata"]["resourceVersion"],
            "annotations": {
                ENFORCER.CANDIDATE_ANNOTATION: "c" * 64,
                ENFORCER.NORMAL_ANNOTATION: topology["policy_names"]["proxy_normal"],
                ENFORCER.DENY_ANNOTATION: topology["policy_names"]["default_deny"],
                ENFORCER.FENCE_ANNOTATION: "coordinator:operation:7",
            },
        },
        "spec": receipt["candidate"]["policies"]["public-envoy"]["spec"],
    }
    return {
        "operation": "guard-stage",
        "topology": _topology_evidence(api),
        "target": target_evidence,
        "lease": lease_evidence,
        "receipt": receipt_evidence,
        "patch_type": "merge",
        "patch": patch,
        "patch_sha256": ENFORCER.sha256_json(patch),
        "dry_run": False,
    }


def test_enforcer_requires_peer_uid_and_client_signature_and_signs_response() -> None:
    api, enforcer, client_key, server_key, _recovery_key = _fixture()
    request = _signed_request(client_key, "attest", {})

    response = enforcer.handle_envelope(request, peer_uid=1001, peer_gid=1002)
    server_key.public_key().verify(
        ENFORCER.decode_base64url(response["signature"], expected_bytes=64),
        ENFORCER.canonical(response["signed"]).encode(),
    )
    assert response["signed"]["status"] == "approved"
    assert response["signed"]["result"]["allowed_actions"] == [
        "transition-mutation",
        "set-admission-recovery",
    ]
    assert response["signed"]["result"]["security_user_info_sha256"] == ENFORCER.sha256_json(
        SECURITY_USER_INFO
    )
    assert response["signed"]["result"]["auditor_bootstrap_sha256"] == "7" * 64
    assert response["signed"]["result"]["effective_rbac_subjects_sha256"] == "4" * 64
    assert response["signed"]["result"]["provider_execution_sha256"] == "a" * 64
    assert response["signed"]["result"]["kubernetes_authentication_sha256"] == "b" * 64
    assert response["signed"]["result"]["external_role_bundle_sha256"] == "9" * 64
    assert response["signed"]["result"]["plan_rotation_phase"] == "preapply"
    assert api.patches == []

    with pytest.raises(ENFORCER.EnforcerError, match="peer UID"):
        enforcer.handle_envelope(_signed_request(client_key, "attest", {}), peer_uid=1002, peer_gid=1002)
    with pytest.raises(ENFORCER.EnforcerError, match="peer UID/GID"):
        enforcer.handle_envelope(_signed_request(client_key, "attest", {}), peer_uid=1001, peer_gid=1003)
    tampered = _signed_request(client_key, "attest", {})
    tampered["signed"]["body"] = {"arbitrary": True}
    with pytest.raises(ENFORCER.EnforcerError, match="signature"):
        enforcer.handle_envelope(tampered, peer_uid=1001, peer_gid=1002)


def test_socket_rejects_peer_before_recv_and_enforces_an_absolute_connection_deadline() -> None:
    _api, enforcer, client_key, _server_key, _recovery_key = _fixture()

    class Connection:
        def __init__(self, uid: int, gid: int, chunks: list[bytes]) -> None:
            self.uid = uid
            self.gid = gid
            self.chunks = chunks
            self.timeouts: list[float] = []
            self.recv_called = False
            self.response = b""

        def getsockopt(self, _level: int, _option: int, _length: int) -> bytes:
            return struct.pack("3i", 999, self.uid, self.gid)

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

        def recv(self, _size: int) -> bytes:
            self.recv_called = True
            return self.chunks.pop(0)

        def sendall(self, response: bytes) -> None:
            self.response = response

    rejected = Connection(1002, 1002, [b"never read"])
    with pytest.raises(ENFORCER.EnforcerError, match="peer UID/GID"):
        ENFORCER.serve_connection(rejected, enforcer)
    assert rejected.recv_called is False
    assert rejected.timeouts == []

    payload = ENFORCER.canonical(_signed_request(client_key, "attest", {})).encode()
    accepted = Connection(1001, 1002, [payload, b""])
    ENFORCER.serve_connection(accepted, enforcer)
    assert accepted.timeouts
    assert all(0 < timeout <= ENFORCER.SOCKET_READ_SECONDS for timeout in accepted.timeouts)
    assert accepted.response.endswith(b"\n")

    oversized = Connection(1001, 1002, [b"x" * (ENFORCER.MAX_REQUEST_BYTES + 1)])
    with pytest.raises(ENFORCER.EnforcerError, match="byte bound"):
        ENFORCER.serve_connection(oversized, enforcer)
    assert oversized.timeouts

    slow_drip = Connection(1001, 1002, [b"{"])
    clock_values = iter([0.0, 0.0, 9.0, 11.0])
    with pytest.raises(ENFORCER.EnforcerError, match="absolute deadline"):
        ENFORCER.serve_connection(slow_drip, enforcer, clock=lambda: next(clock_values))
    assert slow_drip.recv_called is True


def test_enforcer_rejects_a_peer_gid_shared_with_the_security_process(monkeypatch: Any) -> None:
    monkeypatch.setattr(ENFORCER.os, "geteuid", lambda: 2000)
    monkeypatch.setattr(ENFORCER.os, "getegid", lambda: 3000)
    monkeypatch.setattr(ENFORCER.os, "getgroups", lambda: [1002, 3000])
    with pytest.raises(SystemExit):
        ENFORCER.parse_arguments(
            [
                "serve",
                "--socket",
                "/run/fs2/security.sock",
                "--security-kubeconfig",
                "/run/fs2/security.kubeconfig",
                "--client-public-key",
                "A" * 43,
                "--server-private-key",
                "/run/fs2/server.key",
                "--recovery-public-key",
                "B" * 43,
                "--peer-uid",
                "1001",
                "--peer-gid",
                "1002",
                "--api-server-sha256",
                "a" * 64,
                "--kube-system-uid",
                "kube-system-uid-000000000000",
            ]
        )


def test_socket_uses_precreated_setgid_parent_without_chown(monkeypatch: Any) -> None:
    source = SCRIPT.read_text()
    assert "os.chown" not in source
    assert "precreated-setgid-02710" in source
    assert "stat.S_IMODE(parent_stat.st_mode) != 0o2710" in source

    parent = ENFORCER.Path("/run/fs2/identity-epoch-001")
    metadata = type(
        "Metadata",
        (),
        {"st_mode": ENFORCER.stat.S_IFDIR | 0o2710, "st_uid": 2000, "st_gid": 1002},
    )()
    monkeypatch.setattr(ENFORCER.os, "geteuid", lambda: 2000)
    monkeypatch.setattr(ENFORCER.Path, "lstat", lambda path: metadata if path == parent else None)
    ENFORCER.assert_security_owned_socket_parent(parent / "security.sock", peer_gid=1002)


def test_enforcer_rejects_arbitrary_protected_patch_and_allows_exact_semantic_patch() -> None:
    api, enforcer, client_key, _server_key, _recovery_key = _fixture()
    body = _transition_body(api)
    arbitrary = copy.deepcopy(body)
    arbitrary["patch"]["spec"] = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
    arbitrary["patch_sha256"] = ENFORCER.sha256_json(arbitrary["patch"])
    with pytest.raises(ENFORCER.EnforcerError, match="intent/spec"):
        enforcer.handle_envelope(
            _signed_request(client_key, "transition-mutation", arbitrary),
            peer_uid=1001,
            peer_gid=1002,
        )
    assert api.patches == []

    dry_run = copy.deepcopy(body)
    dry_run["dry_run"] = True
    dry_response = enforcer.handle_envelope(
        _signed_request(client_key, "transition-mutation", dry_run),
        peer_uid=1001,
        peer_gid=1002,
    )
    assert dry_response["signed"]["result"]["dry_run"] is True
    assert (
        api.get(
            "networkpolicy",
            "fs2-serve-control-plane-public-envoy-transition-guard",
            "envoy-gateway-system",
        )["metadata"]["resourceVersion"]
        == "14"
    )

    response = enforcer.handle_envelope(
        _signed_request(client_key, "transition-mutation", body),
        peer_uid=1001,
        peer_gid=1002,
    )
    assert response["signed"]["result"]["operation"] == "guard-stage"
    assert api.patches[-1][:3] == (
        "networkpolicy",
        "fs2-serve-control-plane-public-envoy-transition-guard",
        "envoy-gateway-system",
    )


def _recovery_body(
    api: FakeAPI,
    recovery_key: Ed25519PrivateKey,
    *,
    mode: str = "audit-warn",
    reference: str = "SEC-1234",
) -> dict[str, Any]:
    receipt_object = api.get("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE)
    receipt = ENFORCER.receipt_from_object(receipt_object)
    lease = api.get("lease", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE)
    receipt_evidence = ENFORCER.object_evidence(receipt_object)
    receipt_evidence.update(
        receipt_sha256=ENFORCER.sha256_json(receipt),
        phase=receipt["phase"],
        intent=receipt.get("intent"),
    )
    lease_evidence = ENFORCER.object_evidence(lease)
    lease_evidence.update(
        holder_identity=lease["spec"]["holderIdentity"],
        lease_transitions=lease["spec"]["leaseTransitions"],
    )
    topology = _topology_evidence(api)
    issued = dt.datetime.now(dt.UTC)
    recovery_value = _public_value(recovery_key)
    approval_payload = {
        "schema": ENFORCER.RECOVERY_APPROVAL_SCHEMA,
        "mode": mode,
        "recovery_reference": reference,
        "cluster": CLUSTER,
        "topology_uid": topology["uid"],
        "topology_sha256": topology["sha256"],
        "receipt_uid": receipt_evidence["uid"],
        "receipt_resource_version": receipt_evidence["resource_version"],
        "receipt_sha256": ENFORCER.sha256_json(receipt),
        "binding": ENFORCER.SecurityEnforcer._recovery_object_evidence(
            api.get("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME)
        ),
        "parameter": ENFORCER.SecurityEnforcer._recovery_object_evidence(
            api.get("configmap", ENFORCER.PARAMETER_NAME, ENFORCER.RELEASE_NAMESPACE)
        ),
        "issued_at": issued.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "expires_at": (issued + dt.timedelta(minutes=4)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "signer_key_id": ENFORCER.sha256_text(recovery_value),
    }
    approval = {
        "signed": approval_payload,
        "signature": base64.urlsafe_b64encode(recovery_key.sign(ENFORCER.canonical(approval_payload).encode()))
        .decode()
        .rstrip("="),
    }
    return {
        "mode": mode,
        "recovery_reference": reference,
        "topology": topology,
        "receipt": receipt_evidence,
        "lease": lease_evidence,
        "binding": {"name": ENFORCER.ADMISSION_NAME},
        "parameter": {"namespace": ENFORCER.RELEASE_NAMESPACE, "name": ENFORCER.PARAMETER_NAME},
        "approval": approval,
        "delete_allowed": False,
    }


def test_recovery_requires_detached_approval_and_resumes_after_injected_failure() -> None:
    api, enforcer, client_key, _server_key, recovery_key = _fixture()
    receipt_object = api.objects[("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE)]
    receipt = ENFORCER.receipt_from_object(receipt_object)
    receipt["phase"] = "active"
    receipt["intent"] = None
    receipt_object["data"]["receipt.json"] = ENFORCER.canonical(receipt)

    rejected = _recovery_body(api, recovery_key)
    rejected["approval"]["signed"]["recovery_reference"] = "SEC-9999"
    with pytest.raises(ENFORCER.EnforcerError, match="signature"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", rejected),
            peer_uid=1001,
            peer_gid=1002,
        )
    assert api.patches == []

    api.fail_once = ("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, "")
    first = _recovery_body(api, recovery_key)
    with pytest.raises(ENFORCER.EnforcerError, match="injected"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", first),
            peer_uid=1001,
            peer_gid=1002,
        )
    durable = ENFORCER.receipt_from_object(api.get("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE))[
        "security_recovery"
    ]
    assert durable["state"] == "intent"
    assert durable["before"]["binding"]["uid"] == "binding-uid"
    parameter_event = api.events.index(f"patch:configmap:{ENFORCER.PARAMETER_NAME}")
    receipt_events = [
        index for index, event in enumerate(api.events) if event == f"patch:configmap:{ENFORCER.STATE_NAME}"
    ]
    assert max(receipt_events) < parameter_event

    resumed = _recovery_body(api, recovery_key)
    resumed["approval"] = first["approval"]
    response = enforcer.handle_envelope(
        _signed_request(client_key, "set-admission-recovery", resumed),
        peer_uid=1001,
        peer_gid=1002,
    )
    final_receipt = ENFORCER.receipt_from_object(response["signed"]["result"]["receipt"])
    assert final_receipt["security_recovery"]["state"] == "complete"
    assert final_receipt["security_recovery"]["authorization"]["approval_sha256"] == ENFORCER.sha256_json(
        first["approval"]
    )
    assert response["signed"]["result"]["binding"]["spec"]["validationActions"] == ["Audit", "Warn"]
    assert response["signed"]["result"]["parameter"]["data"]["mode"] == "audit-warn"
    assert all(patch[0] != "delete" for patch in api.patches)


def test_recovery_signature_is_not_interchangeable_with_client_signature() -> None:
    api, enforcer, client_key, _server_key, recovery_key = _fixture()
    body = _recovery_body(api, recovery_key)
    body["approval"]["signature"] = (
        base64.urlsafe_b64encode(client_key.sign(ENFORCER.canonical(body["approval"]["signed"]).encode()))
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ENFORCER.EnforcerError, match="approval signature"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", body),
            peer_uid=1001,
            peer_gid=1002,
        )


def test_recovery_approval_rejects_changed_old_admission_object() -> None:
    api, enforcer, client_key, _server_key, recovery_key = _fixture()
    body = _recovery_body(api, recovery_key)
    binding = api.objects[("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, "")]
    binding["metadata"]["resourceVersion"] = "99"
    binding["spec"]["validationActions"] = ["Audit", "Warn"]

    with pytest.raises(ENFORCER.EnforcerError, match="different live state"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", body),
            peer_uid=1001,
            peer_gid=1002,
        )
    assert api.patches == []


def test_recovery_intent_rejects_drift_outside_exact_intermediate_state() -> None:
    api, enforcer, client_key, _server_key, recovery_key = _fixture()
    api.fail_once = ("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, "")
    body = _recovery_body(api, recovery_key)
    with pytest.raises(ENFORCER.EnforcerError, match="injected"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", body),
            peer_uid=1001,
            peer_gid=1002,
        )
    patch_count = len(api.patches)
    binding = api.objects[("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, "")]
    binding["spec"]["unexpected"] = {"preserved": "drift"}
    resumed = _recovery_body(api, recovery_key)
    resumed["approval"] = body["approval"]
    with pytest.raises(ENFORCER.EnforcerError, match="approved before/intermediate/target"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", resumed),
            peer_uid=1001,
            peer_gid=1002,
        )
    assert len(api.patches) == patch_count


def test_completed_recovery_is_bounded_read_only_and_rejects_full_object_drift() -> None:
    api, enforcer, client_key, _server_key, recovery_key = _fixture()
    body = _recovery_body(api, recovery_key)
    response = enforcer.handle_envelope(
        _signed_request(client_key, "set-admission-recovery", body),
        peer_uid=1001,
        peer_gid=1002,
    )
    patch_count = len(api.patches)

    replay = _recovery_body(api, recovery_key)
    replay["approval"] = body["approval"]
    replayed = enforcer.handle_envelope(
        _signed_request(client_key, "set-admission-recovery", replay),
        peer_uid=1001,
        peer_gid=1002,
    )
    assert len(api.patches) == patch_count
    assert replayed["signed"]["result"]["receipt"] == response["signed"]["result"]["receipt"]

    binding = api.objects[("validatingadmissionpolicybinding", ENFORCER.ADMISSION_NAME, "")]
    binding["metadata"]["annotations"] = {"unexpected": "drift"}
    with pytest.raises(ENFORCER.EnforcerError, match="terminal.*drifted"):
        enforcer.handle_envelope(
            _signed_request(client_key, "set-admission-recovery", replay),
            peer_uid=1001,
            peer_gid=1002,
        )
    assert len(api.patches) == patch_count


def test_server_response_signature_cannot_authorize_a_request() -> None:
    _api, enforcer, _client_key, server_key, _recovery_key = _fixture()
    forged = _signed_request(server_key, "attest", {})
    forged["signed"]["client_key_id"] = enforcer.client_key_id
    forged["signature"] = (
        base64.urlsafe_b64encode(server_key.sign(ENFORCER.canonical(forged["signed"]).encode())).decode().rstrip("=")
    )
    with pytest.raises(ENFORCER.EnforcerError, match="request signature"):
        enforcer.handle_envelope(forged, peer_uid=1001, peer_gid=1002)

    with pytest.raises(InvalidSignature):
        enforcer.client_public_key.verify(
            ENFORCER.decode_base64url(forged["signature"], expected_bytes=64),
            ENFORCER.canonical(forged["signed"]).encode(),
        )


def test_recovery_approval_signer_reads_exact_live_state_without_mutation(monkeypatch: Any) -> None:
    api, _enforcer, _client_key, _server_key, recovery_key = _fixture()
    monkeypatch.setattr(APPROVAL, "load_private_key", lambda _path: recovery_key)

    envelope = APPROVAL.create_approval(
        api,
        private_key_path=APPROVAL.Path("/security/recovery.key"),
        mode="deny",
        recovery_reference="CHG-1234",
    )

    recovery_key.public_key().verify(
        ENFORCER.decode_base64url(envelope["signature"], expected_bytes=64),
        ENFORCER.canonical(envelope["signed"]).encode(),
    )
    receipt = ENFORCER.receipt_from_object(api.get("configmap", ENFORCER.STATE_NAME, ENFORCER.RELEASE_NAMESPACE))
    assert envelope["signed"]["receipt_sha256"] == ENFORCER.sha256_json(receipt)
    assert envelope["signed"]["topology_uid"] == "topology-uid"
    assert envelope["signed"]["binding"]["uid"] == "binding-uid"
    assert envelope["signed"]["parameter"]["uid"] == "parameter-uid"
    assert api.patches == []
