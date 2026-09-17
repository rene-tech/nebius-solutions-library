# ruff: noqa: S603 -- fixed repository helper and test-owned command fakes.
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from conftest import CONTROL_ROOT
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

SCRIPT = CONTROL_ROOT / "scripts" / "network_policy_transition.py"
WRAPPER = CONTROL_ROOT / "scripts" / "network-policy-transition.sh"
SOLUTION_ROOT = CONTROL_ROOT.parents[1]
CHART = SOLUTION_ROOT / "charts" / "control-plane" / "fs2-serve-control-plane"
TERRAFORM = SOLUTION_ROOT / "stages" / "workloads" / "control_plane.tf"
BOUNDARY_TERRAFORM = SOLUTION_ROOT / "stages" / "foundation" / "control_plane_network_policy_boundary.tf"
BOUNDARY_PREFLIGHT = (
    SOLUTION_ROOT / "stages" / "foundation" / "scripts" / "verify-network-policy-security-preflight.py"
)
HANDOFF_SCHEMA = CONTROL_ROOT / "contracts" / "network-policy-security-handoff-v2.schema.json"
SUBJECT_INVENTORY_SCHEMA = (
    CONTROL_ROOT / "contracts" / "network-policy-security-subject-inventory-v3.schema.json"
)
PROVIDER_SNAPSHOT_SCHEMA = (
    CONTROL_ROOT / "contracts" / "network-policy-security-subject-provider-snapshot-v3.schema.json"
)
PROVIDER_TRUST_ANCHOR_SCHEMA = (
    CONTROL_ROOT / "contracts" / "network-policy-security-provider-trust-anchor-v3.schema.json"
)
PROVIDER_COLLECTION_AUTHORITY_SCHEMA = (
    CONTROL_ROOT / "contracts" / "network-policy-security-provider-collection-authority-v1.schema.json"
)
PROVIDER_ADAPTER = CONTROL_ROOT / "scripts" / "network_policy_subject_provider_adapter.py"
EPOCH_RETIREMENT = (
    SOLUTION_ROOT / "stages" / "foundation" / "scripts" / "verify-network-policy-epoch-retirement.py"
)
ENFORCER_SCRIPT = CONTROL_ROOT / "scripts" / "network_policy_security_enforcer.py"


def _load_transition_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fs2_network_policy_transition", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TRANSITION = _load_transition_module()


def _load_preflight_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fs2_network_policy_security_preflight", BOUNDARY_PREFLIGHT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PREFLIGHT = _load_preflight_module()


def _load_provider_adapter_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fs2_network_policy_subject_provider_adapter", PROVIDER_ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROVIDER_ADAPTER_MODULE = _load_provider_adapter_module()

PROXY_SPEC = {
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "envoy"}},
    "policyTypes": ["Ingress", "Egress"],
    "ingress": [{"ports": [{"port": 10080, "protocol": "TCP"}]}],
    "egress": [{"ports": [{"port": 53, "protocol": "UDP"}]}],
}
CONTROLLER_SPEC = {
    "podSelector": {"matchLabels": {"control-plane": "envoy-gateway"}},
    "policyTypes": ["Ingress"],
    "ingress": [{"ports": [{"port": 18000, "protocol": "TCP"}]}],
}
DEPLOYED_VALUES = {"config": {"requestDebugEnabled": False}}


def _candidate() -> Any:
    proxy = {
        "namespace": "edge-custom",
        "normal_name": "test-release-public-envoy",
        "guard_name": "test-release-public-envoy-transition-guard",
        "spec": PROXY_SPEC,
    }
    controller = {
        "namespace": "controller-custom",
        "normal_name": "test-release-envoy-controller-xds",
        "guard_name": "test-release-envoy-controller-xds-transition-guard",
        "spec": CONTROLLER_SPEC,
    }
    return TRANSITION.Candidate(
        candidate_sha256="a" * 64,
        chart_sha256="b" * 64,
        values_sha256="c" * 64,
        complete_render_sha256="e" * 64,
        network_policy_render_sha256="d" * 64,
        release={
            "name": "test-release",
            "namespace": "fs2-system",
            "revision": "138",
            "status": "deployed",
            "deployed_manifest_sha256": "e" * 64,
            "deployed_values_sha256": TRANSITION.sha256_json(DEPLOYED_VALUES),
            "request_debug_enabled": False,
            "identity_uid": "uid-helm-release-138",
            "storage": {
                "name": "sh.helm.release.v1.test-release.v138",
                "uid": "uid-helm-release-138",
                "resource_version": "1380",
                "revision": "138",
                "status": "deployed",
            },
            "objects": {
                "public-envoy": {"uid": "uid-public-envoy-normal"},
                "envoy-controller": {"uid": "uid-envoy-controller-normal"},
            },
        },
        topology={
            "contract": {
                "schema": "fs2-serve.nebius.ai/network-policy-boundary-topology/v1",
                "mode": "public",
                "security_owner_username": "fs2-np-security-owner-0123456789abcdef",
                "gateway_namespace": "edge-custom",
                "controller_namespace": "controller-custom",
                "policy_names": {
                    "proxy_normal": "test-release-public-envoy",
                    "proxy_guard": "test-release-public-envoy-transition-guard",
                    "controller_normal": "test-release-envoy-controller-xds",
                    "controller_guard": "test-release-envoy-controller-xds-transition-guard",
                    "default_deny": "test-release-envoy-default-deny",
                },
            },
            "uid": "topology-uid",
            "resource_version": "11",
            "sha256": "f" * 64,
        },
        proxy=proxy,
        controller=controller,
        deny_name="test-release-envoy-default-deny",
    )


def _boundary(role: str, name: str, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{role}",
            "resourceVersion": "17",
            "labels": {
                TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE,
                TRANSITION.ROLE_LABEL: role,
            },
            "annotations": {TRANSITION.CANDIDATE_ANNOTATION: "a" * 64},
        },
        "spec": spec,
    }


class FakeCommand:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, ...]] = []

    def run(self, *arguments: str, **kwargs: Any) -> Any:
        self.calls.append(arguments)
        return self.handler(arguments, kwargs)


def _result(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    return TRANSITION.CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _bare_transition() -> Any:
    transition = object.__new__(TRANSITION.Transition)
    transition.release = "test-release"
    transition.release_namespace = "fs2-system"
    transition.arguments = type(
        "Arguments",
        (),
        {"revision": "7", "timeout": "10m", "recovery_reference": "", "recovery_approval": ""},
    )()
    transition.holder = "test-holder"
    transition.fence_transitions = 3
    transition.lock = contextlib.nullcontext
    transition._renew_fence = lambda: {}
    return transition


def test_chart_cannot_render_or_own_permanent_boundary() -> None:
    template = (CHART / "templates" / "networkpolicy.yaml").read_text()
    values = (CHART / "values.yaml").read_text()
    schema = (CHART / "values.schema.json").read_text()

    for forbidden in (
        "renderGuards",
        "public-envoy-transition-guard",
        "envoy-controller-xds-transition-guard",
        "envoy-default-deny",
        "network-policy-boundary",
    ):
        assert forbidden not in template
    assert "renderGuards" not in values
    assert "renderGuards" not in schema


def test_wrapper_delegates_to_state_machine_without_cluster_wide_access() -> None:
    wrapper = WRAPPER.read_text()
    source = SCRIPT.read_text()
    enforcer = ENFORCER_SCRIPT.read_text()

    assert 'exec python3 "${script_dir}/network_policy_transition.py" "$@"' in wrapper
    assert "--all-namespaces" not in source
    assert '"--namespace", namespace' in source
    assert '"create",\n            "token"' not in source
    assert "SERVICE_ACCOUNT" not in source
    assert 'parser.add_argument("--security-handoff-socket", required=True)' in source
    assert 'parser.add_argument("--security-handoff-server-public-key", required=True)' in source
    assert 'parser.add_argument("--security-handoff-client-public-key", required=True)' in source
    assert 'parser.add_argument("--security-handoff-client-private-key", required=True)' in source
    assert "security_owner_kubeconfig" not in source
    assert 'action not in {"attest", "transition-mutation", "set-admission-recovery"}' in source
    schema = json.loads(HANDOFF_SCHEMA.read_text())
    request_action = schema["$defs"]["request"]["properties"]["action"]["enum"]
    assert request_action == ["attest", "transition-mutation", "set-admission-recovery"]
    assert "delete" not in request_action
    assert "assert_security_owned_file(kubeconfig" in enforcer
    assert "assert_security_owned_socket_parent(socket_path, peer_gid=peer_gid)" in enforcer
    assert "os.chown" not in enforcer
    assert "security enforcer and rollout peer must use distinct Unix UIDs" in enforcer
    assert 'arguments.extend([f"--type={patch_type}", "--patch", canonical(patch)])' in enforcer
    assert '"delete"' not in enforcer.split("class KubectlAPI:", maxsplit=1)[1].split(
        "def object_evidence", maxsplit=1
    )[0]


def test_signed_handoff_schema_rejects_generic_or_delete_shaped_requests() -> None:
    schema = json.loads(HANDOFF_SCHEMA.read_text())
    validator = Draft202012Validator(schema)
    base = {
        "schema": "fs2-serve.nebius.ai/network-policy-security-handoff-request/v2",
        "operation_id": "b33f7e4c-bf2f-48ec-a0d3-df64f2393880",
        "client_key_id": "d" * 64,
        "cluster": {"api_server_sha256": "a" * 64, "kube_system_uid": "8af2b70d-25f7-4f0e-9ad0-776abc8a61e2"},
        "release": {"name": "fs2-serve-control-plane", "namespace": "fs2-system"},
        "issued_at": "2026-09-16T16:00:00Z",
        "expires_at": "2026-09-16T16:00:30Z",
    }
    request = {
        **base,
        "action": "transition-mutation",
        "body": {
            "operation": "lease-renew",
            "topology": {
                "namespace": "fs2-system",
                "name": "fs2-network-policy-boundary-topology",
                "uid": "8af2b70d-25f7-4f0e-9ad0-776abc8a61e2",
                "resource_version": "18",
                "sha256": "b" * 64,
            },
            "target": {
                "api_version": "coordination.k8s.io/v1",
                "kind": "Lease",
                "namespace": "fs2-system",
                "name": "fs2-network-policy-transition",
                "uid": "e31e7ee2-a6f7-4c76-a48a-c10662a0622e",
                "resource_version": "21",
                "state_sha256": "c" * 64,
            },
            "patch_type": "merge",
            "patch": {"metadata": {"resourceVersion": "17"}},
            "patch_sha256": "e" * 64,
            "dry_run": True,
        },
    }
    envelope = {"signed": request, "signature": "A" * 86}

    assert validator.is_valid(envelope)
    assert not validator.is_valid(request)
    assert not validator.is_valid({"signed": {**request, "action": "delete"}, "signature": "A" * 86})
    arbitrary = {**request["body"], "resource": "foreign-policy"}
    assert not validator.is_valid({"signed": {**request, "body": arbitrary}, "signature": "A" * 86})


def test_rollout_identity_cannot_mutate_any_protected_boundary() -> None:
    transition = _bare_transition()
    topology = {
        "security_owner_username": "external-security-owner",
        "gateway_namespace": "edge-custom",
        "controller_namespace": "controller-custom",
        "policy_names": {
            "proxy_guard": "proxy-guard",
            "controller_guard": "controller-guard",
            "default_deny": "default-deny",
        },
    }

    def denied(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert arguments[:2] == ("auth", "can-i")
        if arguments[2] == "get" and "--namespace" in arguments:
            return _result(stdout="yes\n")
        return _result(stdout="no\n")

    transition.bootstrap_kubectl = FakeCommand(denied)
    transition.verify_external_iam_boundary(topology)
    assert any(
        "patch" in call and "--resource-name=fs2-network-policy-boundary" in call
        for call in transition.bootstrap_kubectl.calls
    )
    assert any("proxy-guard" in item for call in transition.bootstrap_kubectl.calls for item in call)
    parameter_name = "fs2-network-policy-boundary-parameters"
    assert any(parameter_name in item for call in transition.bootstrap_kubectl.calls for item in call)
    assert any("deletecollection" in call for call in transition.bootstrap_kubectl.calls)
    assert any("fs2-network-policy-security-probe" in call for call in transition.bootstrap_kubectl.calls)
    assert any("system:authenticated" in call for call in transition.bootstrap_kubectl.calls)
    assert any(
        call[2] == "bind" and not any(item.startswith("--resource-name=") for item in call)
        for call in transition.bootstrap_kubectl.calls
    )
    assert any(
        call[2] == "escalate" and not any(item.startswith("--resource-name=") for item in call)
        for call in transition.bootstrap_kubectl.calls
    )
    assert any("--subresource=token" in call for call in transition.bootstrap_kubectl.calls)
    assert all(call[:2] == ("auth", "can-i") for call in transition.bootstrap_kubectl.calls)

    def allowed(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        return _result(stdout="yes\n")

    transition.bootstrap_kubectl = FakeCommand(allowed)
    with pytest.raises(TRANSITION.TransitionError, match="external security boundary"):
        transition.verify_external_iam_boundary(topology)


def test_signed_handoff_must_bind_the_same_cluster_identity() -> None:
    transition = _bare_transition()
    epoch = "identity-epoch-001"
    prior_epoch = "identity-epoch-000"
    successor_epoch = "identity-epoch-002"

    def principal(role: str, value: str) -> str:
        return f"fs2-np-{role}-{TRANSITION.hashlib.sha256(value.encode()).hexdigest()[:16]}"

    public_key = TRANSITION.base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    transition.security_handoff_socket = TRANSITION.Path("/run/fs2/identity-epoch-001/security.sock")
    transition.security_handoff_server_public_key = public_key
    transition.security_handoff_client_public_key = public_key
    transition.security_handoff_client_private_key = TRANSITION.Path("/run/fs2/client.key")
    release_info = {
        "username": principal("release", epoch),
        "uid": "release-uid",
        "groups": ["system:authenticated"],
        "extra": {},
    }
    transition.bootstrap_kubectl = FakeCommand(
        lambda arguments, _kwargs: _result(
            stdout=json.dumps({"status": {"userInfo": release_info}}) if arguments[:2] == ("auth", "whoami") else ""
        )
    )
    transition._cluster_identity = lambda _command: ("https://api-one", "uid-one")
    transition._protected_topology = lambda _command: {
        "contract": {
            "security_owner_username": principal("security-owner", epoch),
            "security_bootstrap_username": principal("security-bootstrap", epoch),
            "successor_security_owner_username": principal("security-owner", successor_epoch),
            "successor_security_bootstrap_username": principal("security-bootstrap", successor_epoch),
            "security_handoff": {
                "schema": "fs2-serve.nebius.ai/network-policy-security-handoff/v2",
                "socket_path": "/run/fs2/identity-epoch-001/security.sock",
                "socket_directory_contract": "precreated-setgid-02710",
                "server_public_key": public_key,
                "server_public_key_sha256": TRANSITION.sha256_text(public_key),
                "client_public_key": public_key,
                "client_public_key_sha256": TRANSITION.sha256_text(public_key),
                "client_private_key_path": "/run/fs2/client.key",
                "recovery_public_key_sha256": "e" * 64,
                "peer_uid": TRANSITION.os.geteuid(),
                "peer_gid": TRANSITION.os.getegid(),
                "peer_gid_contract": "effective-dedicated",
                "identity_boundary": {
                    "schema": "fs2-serve.nebius.ai/network-policy-identity-boundary/v3",
                    "identity_epoch": epoch,
                    "prior_identity_epoch": prior_epoch,
                    "successor_identity_epoch": successor_epoch,
                    "epoch_principals": {
                        "release": principal("release", epoch),
                        "security_owner": principal("security-owner", epoch),
                        "security_bootstrap": principal("security-bootstrap", epoch),
                        "prior_owner": principal("security-owner", prior_epoch),
                        "prior_bootstrap": principal("security-bootstrap", prior_epoch),
                        "successor_owner": principal("security-owner", successor_epoch),
                        "successor_bootstrap": principal("security-bootstrap", successor_epoch),
                    },
                    "release_user_info_sha256": TRANSITION.sha256_json(release_info),
                    "security_user_info_sha256": "d" * 64,
                    "bootstrap_user_info_sha256": "e" * 64,
                    "prior_security_user_info_sha256": "0" * 64,
                    "prior_bootstrap_user_info_sha256": "1" * 64,
                    "credential_set_sha256": "b" * 64,
                    "release_kubeconfig_sha256": "c" * 64,
                    "security_kubeconfig_sha256": "d" * 64,
                    "bootstrap_kubeconfig_sha256": "e" * 64,
                    "prior_security_kubeconfig_sha256": "4" * 64,
                    "prior_bootstrap_kubeconfig_sha256": "5" * 64,
                    "security_subject_inventory_sha256": "f" * 64,
                    "provider_subject_snapshot_sha256": "2" * 64,
                    "provider_trust_anchor_sha256": "6" * 64,
                    "provider_collection_authority_sha256": "7" * 64,
                    "provider_adapter_sha256": "7" * 64,
                    "provider_execution_sha256": "8" * 64,
                    "kubernetes_authentication_sha256": "9" * 64,
                    "oidc_mapping_sha256": "a" * 64,
                    "kubernetes_subject_inventory_sha256": "3" * 64,
                    "kubernetes_subject_inventory_post_sar_sha256": "3" * 64,
                    "effective_rbac_subjects_sha256": "4" * 64,
                    "auditor_bootstrap_sha256": "8" * 64,
                    "external_role_bundle_sha256": "1" * 64,
                    "plan_rotation_phase": "preapply",
                    "rotation_binding_state_sha256": "9" * 64,
                    "plan_preflight_verified": True,
                    "plan_preflight_sha256": "a" * 64,
                    "release_expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).isoformat(),
                    "security_expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).isoformat(),
                    "bootstrap_expires_at": (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat(),
                    "rollback_valid_until": (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat(),
                    "minimum_rollback_seconds": 3600,
                    "rotation_contract": {
                        "mechanism": "preauthorized-successor-epoch",
                        "bootstrap_update_identity": principal("security-bootstrap", epoch),
                        "successor_security_owner_identity": principal("security-owner", successor_epoch),
                        "successor_bootstrap_identity": principal("security-bootstrap", successor_epoch),
                        "prior_security_owner_identity": principal("security-owner", prior_epoch),
                        "prior_bootstrap_identity": principal("security-bootstrap", prior_epoch),
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
                "cluster": {"api_server_sha256": "different", "kube_system_uid": "uid-two"},
                "allowed_actions": ["transition-mutation", "set-admission-recovery"],
                "delete_allowed": False,
                "recovery_modes": ["Audit", "Warn", "Deny"],
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
                    "immutable_role_bundle_sha256": "1" * 64,
                    "preapply_subjects": [
                        principal("security-bootstrap", prior_epoch),
                        principal("security-bootstrap", epoch),
                    ],
                    "desired_subjects": [
                        principal("security-bootstrap", epoch),
                        principal("security-bootstrap", successor_epoch),
                    ],
                    "observed_sha256": "8" * 64,
                    "bootstrap_create": False,
                    "bootstrap_exact_update": True,
                    "delete_allowed": False,
                },
            },
        }
    }

    with pytest.raises(TRANSITION.TransitionError, match="same-cluster topology"):
        transition.configure_guarded_client()


def test_signed_handoff_verifies_request_cluster_expiry_and_ed25519_signature(
    monkeypatch: Any,
) -> None:
    server_private_key = Ed25519PrivateKey.generate()
    server_public_key = (
        TRANSITION.base64.urlsafe_b64encode(server_private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    client_private_key = Ed25519PrivateKey.generate()
    client_public_key = (
        TRANSITION.base64.urlsafe_b64encode(client_private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    cluster = {"api_server_sha256": "a" * 64, "kube_system_uid": "cluster-uid"}
    handoff = object.__new__(TRANSITION.SecurityHandoff)
    handoff.socket_path = TRANSITION.Path("/run/fs2/security.sock")
    handoff.server_public_key = server_private_key.public_key()
    handoff.server_key_id = TRANSITION.sha256_text(server_public_key)
    handoff.client_private_key = client_private_key
    handoff.client_key_id = TRANSITION.sha256_text(client_public_key)
    handoff.cluster = cluster
    handoff.release = {"name": "test-release", "namespace": "fs2-system"}

    def exchange(envelope: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        assert deadline > TRANSITION.time.monotonic()
        request = envelope["signed"]
        client_private_key.public_key().verify(
            TRANSITION.decode_base64url(envelope["signature"], expected_bytes=64),
            TRANSITION.canonical(request).encode(),
        )
        issued = TRANSITION.dt.datetime.now(TRANSITION.dt.UTC)
        signed = {
            "schema": "fs2-serve.nebius.ai/network-policy-security-handoff-response/v2",
            "operation_id": request["operation_id"],
            "request_sha256": TRANSITION.sha256_json(request),
            "cluster": cluster,
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "expires_at": (issued + TRANSITION.dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "status": "approved",
            "signer_key_id": handoff.server_key_id,
            "result": {"attested": True},
        }
        signature = TRANSITION.base64.urlsafe_b64encode(server_private_key.sign(TRANSITION.canonical(signed).encode()))
        return {"signed": signed, "signature": signature.decode().rstrip("=")}

    handoff._exchange = exchange
    assert handoff.request("attest", {}) == {"attested": True}

    handoff._exchange = lambda request, *, deadline: {
        **exchange(request, deadline=deadline),
        "signature": "A" * 86,
    }
    with pytest.raises(TRANSITION.TransitionError, match="signature is invalid"):
        handoff.request("attest", {})

    handoff._exchange = exchange
    monotonic_ticks = iter([0.0, 1.0, TRANSITION.HANDOFF_SECONDS + 1.0])
    monkeypatch.setattr(TRANSITION.time, "monotonic", lambda: next(monotonic_ticks))
    with pytest.raises(TRANSITION.TransitionError, match="monotonic end-to-end deadline"):
        handoff.request("attest", {})


def test_protected_command_routes_only_exact_patches_to_signed_handoff() -> None:
    topology_contract = {
        "policy_names": {
            "proxy_guard": "guard",
            "controller_guard": "controller-guard",
            "default_deny": "default-deny",
        }
    }
    topology_resource = {
        "metadata": {
            "name": "fs2-network-policy-boundary-topology",
            "namespace": "fs2-system",
            "uid": "topology-uid",
            "resourceVersion": "11",
        }
    }
    receipt = {"phase": "guards-staging", "intent": {"operation": "stage-guards"}}
    objects = {
        ("networkpolicy", "guard", "edge-custom"): {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "guard",
                "namespace": "edge-custom",
                "uid": "guard-uid",
                "resourceVersion": "17",
                "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
            },
            "spec": {},
        },
        ("lease", TRANSITION.LEASE_NAME, "fs2-system"): {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": TRANSITION.LEASE_NAME,
                "namespace": "fs2-system",
                "uid": "lease-uid",
                "resourceVersion": "19",
                "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
            },
            "spec": {"holderIdentity": "holder", "leaseTransitions": 3},
        },
        ("configmap", TRANSITION.RECEIPT_NAME, "fs2-system"): {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": TRANSITION.RECEIPT_NAME,
                "namespace": "fs2-system",
                "uid": "receipt-uid",
                "resourceVersion": "20",
                "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
            },
            "data": {"receipt.json": TRANSITION.canonical(receipt)},
        },
    }

    def ordinary_handler(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        if arguments[0] != "get":
            return _result(stdout='{"kind":"ConfigMap"}')
        resource, name = arguments[1:3]
        namespace = arguments[arguments.index("--namespace") + 1]
        resource_object = objects.get((resource, name, namespace), {"kind": "ConfigMap"})
        return _result(stdout=json.dumps(resource_object))

    ordinary = FakeCommand(ordinary_handler)

    class Handoff:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.object_name = "guard"

        def request(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((action, body))
            return {
                "object": {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": {
                        "name": self.object_name,
                        "namespace": "edge-custom",
                        "uid": "guard-uid",
                        "resourceVersion": "18",
                        "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
                    },
                }
            }

    handoff = Handoff()
    command = TRANSITION.ProtectedCommand(
        ordinary,
        handoff,
        allowed_patch_targets={("networkpolicy", "guard", "edge-custom")},
        topology={"resource": topology_resource, "contract": topology_contract},
    )
    assert (
        json.loads(command.run("get", "configmap", "name", "--namespace", "fs2-system", "-o", "json").stdout)["kind"]
        == "ConfigMap"
    )
    patched = command.run(
        "patch",
        "networkpolicy",
        "guard",
        "--namespace",
        "edge-custom",
        "--type=merge",
        "--patch",
        '{"metadata":{"resourceVersion":"17"},"spec":{}}',
        "--dry-run=server",
        "-o",
        "json",
    )
    assert json.loads(patched.stdout)["kind"] == "NetworkPolicy"
    assert handoff.calls[0][0] == "transition-mutation"
    assert handoff.calls[0][1]["operation"] == "guard-stage"
    assert handoff.calls[0][1]["target"]["uid"] == "guard-uid"
    assert handoff.calls[0][1]["receipt"]["phase"] == "guards-staging"
    assert handoff.calls[0][1]["dry_run"] is True
    with pytest.raises(TRANSITION.TransitionError, match="signed patch handoff"):
        command.run("delete", "networkpolicy", "guard", "--namespace", "edge-custom")
    with pytest.raises(TRANSITION.TransitionError, match="allowlist"):
        command.run(
            "patch",
            "networkpolicy",
            "different",
            "--namespace",
            "edge-custom",
            "--type=merge",
            "--patch",
            "{}",
        )
    handoff.object_name = "different"
    with pytest.raises(TRANSITION.TransitionError, match="different protected object"):
        command.run(
            "patch",
            "networkpolicy",
            "guard",
            "--namespace",
            "edge-custom",
            "--type=merge",
            "--patch",
            '{"metadata":{"resourceVersion":"17"},"spec":{}}',
        )


def test_ready_coverage_requires_a_ready_selected_pod() -> None:
    transition = _bare_transition()

    def no_pods(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert arguments[:2] == ("get", "--raw")
        assert "/api/v1/namespaces/edge-custom/pods?limit=100&" in arguments[2]
        return _result(stdout='{"metadata":{"continue":""},"items":[]}')

    transition.guarded_kubectl = FakeCommand(no_pods)
    with pytest.raises(TRANSITION.TransitionError, match="selects zero Ready Pods"):
        transition.verify_ready_pods("edge-custom", PROXY_SPEC, role="public-envoy")

    def ready_pod(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert arguments[:2] == ("get", "--raw")
        assert "labelSelector=app.kubernetes.io%2Fname%3Denvoy" in arguments[2]
        return _result(
            stdout=json.dumps(
                {
                    "metadata": {"continue": ""},
                    "items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}],
                }
            )
        )

    transition.guarded_kubectl = FakeCommand(ready_pod)
    assert transition.verify_ready_pods("edge-custom", PROXY_SPEC, role="public-envoy") == 1


def test_strict_stage_rejects_absent_release_before_any_boundary_mutation() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    candidate.release["status"] = "absent"
    transition.render_candidate = lambda: candidate

    with pytest.raises(TRANSITION.TransitionError, match="requires a successful deployed release"):
        transition.stage()


def test_first_install_prepare_keeps_deny_relaxed_until_post_install_complete() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    candidate.release["status"] = "absent"
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {"matchLabels": TRANSITION.RELAXED_SELECTOR}, "policyTypes": ["Ingress"]},
    )
    writes: list[tuple[str, dict[str, Any]]] = []
    transition.render_candidate = lambda: candidate
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: ({}, {"phase": "uninitialized"})
    transition.get_policy = lambda name, _namespace: {
        candidate.proxy["guard_name"]: proxy,
        candidate.controller["guard_name"]: controller,
        candidate.deny_name: deny,
    }[name]
    transition.relax_deny = lambda _candidate: "relaxed-zero-selected-pods"
    transition.write_receipt = lambda phase, *_args, **kwargs: writes.append((phase, kwargs)) or {}

    transition.prepare()

    assert [phase for phase, _kwargs in writes] == ["bootstrap-relaxing", "bootstrap-ready"]
    assert writes[0][1]["extra"]["intent"]["operation"] == "relax-deny"
    assert writes[1:] == [
        (
            "bootstrap-ready",
            {"extra": {"bootstrap": {"deny_proof": "relaxed-zero-selected-pods"}}},
        )
    ]


def test_stage_rejects_a_different_candidate_while_prior_transition_is_in_flight() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    prior = _candidate()
    prior.candidate_sha256 = "f" * 64
    transition.receipt = lambda: (
        {},
        {"phase": "staged", "candidate": prior.as_dict()},
    )

    with pytest.raises(TRANSITION.TransitionError, match="still in flight"):
        transition._stage_locked(candidate)


def test_stage_persists_guard_and_deny_intents_before_each_policy_mutation() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    transition.receipt = lambda: ({}, {"phase": "uninitialized"})
    transition.boundary_objects = lambda _candidate: (
        {"public-envoy": proxy, "envoy-controller": controller},
        deny,
    )
    events: list[str] = []

    def write(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append(f"receipt:{phase}")
        return {}

    transition.write_receipt = write
    transition.patch_guard = lambda role, *_args: events.append(f"patch:{role}") or (
        proxy if role == "public-envoy" else controller
    )
    transition.activate_deny = lambda _candidate: events.append("patch:deny-active")
    transition._stage_locked(candidate)

    assert events == [
        "receipt:guards-staging",
        "patch:public-envoy",
        "patch:envoy-controller",
        "receipt:guards-ready",
        "patch:deny-active",
        "receipt:staged",
    ]


def test_stage_resumes_partial_guard_mutation_from_uid_bound_intent() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary("default-deny", candidate.deny_name, candidate.proxy["namespace"], {"podSelector": {}})
    receipt = {
        "phase": "guards-staging",
        "candidate": candidate.as_dict(),
        "boundary_objects": {
            "public-envoy": TRANSITION.Transition._guard_receipt(proxy),
            "envoy-controller": TRANSITION.Transition._guard_receipt(controller),
            "default-deny": TRANSITION.Transition._guard_receipt(deny),
        },
    }
    proxy["metadata"]["resourceVersion"] = "18"
    proxy["spec"] = {**PROXY_SPEC, "egress": []}
    transition.receipt = lambda: ({}, receipt)
    transition.boundary_objects = lambda _candidate: (
        {"public-envoy": proxy, "envoy-controller": controller},
        deny,
    )
    patched: list[str] = []
    transition.patch_guard = lambda role, *_args: patched.append(role) or (
        proxy if role == "public-envoy" else controller
    )
    transition.write_receipt = lambda *_args, **_kwargs: {}
    transition.activate_deny = lambda *_args: None

    transition._stage_locked(candidate)

    assert patched == ["public-envoy", "envoy-controller"]


def test_bootstrap_complete_rebinds_ready_guards_before_activating_deny() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    state = {"phase": "bootstrap-ready"}
    verified_phases: list[str] = []
    events: list[str] = []
    transition.render_candidate = lambda: candidate
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: (
        {},
        {
            "phase": state["phase"],
            "candidate": candidate.as_dict(),
        },
    )
    transition.get_policy = lambda name, _namespace: {
        candidate.proxy["guard_name"]: proxy,
        candidate.controller["guard_name"]: controller,
        candidate.deny_name: deny,
    }[name]
    transition.verify_receipt_boundaries = lambda receipt, _boundaries: verified_phases.append(receipt["phase"])
    transition.verify_receipt_boundary_uids = lambda receipt, _boundaries: verified_phases.append(receipt["phase"])
    transition.verify_local_candidate = lambda _receipt: candidate
    deployed_release = {**candidate.release, "revision": "139"}
    transition.verify_deployed_target = lambda _candidate: deployed_release
    transition.capture_rollback_source = lambda _candidate: {"bound": True}
    transition.current_guards = lambda _candidate: {
        "public-envoy": proxy,
        "envoy-controller": controller,
    }

    def patch(role: str, *_args: Any) -> dict[str, Any]:
        events.append(f"ready:{role}")
        return proxy if role == "public-envoy" else controller

    transition.patch_guard = patch
    transition.activate_deny = lambda _candidate: events.append("deny:active")
    transition.verify_normal = lambda role, _policy: events.append(f"normal:{role}")
    transition.verify_deny_active = lambda _candidate: None

    def write(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        state["phase"] = phase
        events.append(f"receipt:{phase}")
        return {}

    transition.write_receipt = write
    transition.complete()

    assert verified_phases == ["bootstrap-ready", "bootstrap-guards-staging", "guards-ready", "staged"]
    assert events[:2] == ["receipt:bootstrap-guards-staging", "ready:public-envoy"]
    assert events.index("receipt:guards-ready") < events.index("deny:active")
    assert events.index("ready:public-envoy") < events.index("deny:active")
    assert events.index("ready:envoy-controller") < events.index("deny:active")
    assert events[-1] == "receipt:active"


def test_complete_resumes_durable_guards_ready_after_deny_activation_crash() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    state = {"phase": "guards-ready"}
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: ({}, {"phase": state["phase"], "candidate": candidate.as_dict()})
    transition.verify_local_candidate = lambda _receipt: candidate
    transition.verify_deployed_target = lambda _candidate: {**candidate.release, "revision": "139"}
    transition.capture_rollback_source = lambda _candidate: {"bound": True}
    transition.current_guards = lambda _candidate: {
        "public-envoy": proxy,
        "envoy-controller": controller,
    }
    transition.get_policy = lambda *_args: deny
    transition.verify_receipt_boundary_uids = lambda *_args: None
    events: list[str] = []
    transition.activate_deny = lambda _candidate: events.append("deny:active")
    transition.verify_normal = lambda *_args: None
    transition.verify_deny_active = lambda *_args: None
    transition.verify_receipt_boundaries = lambda *_args: None

    def write(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        state["phase"] = phase
        events.append(f"receipt:{phase}")
        return {}

    transition.write_receipt = write
    transition.complete()

    assert events[:2] == ["deny:active", "receipt:staged"]
    assert events[-1] == "receipt:active"


def test_lock_rejects_concurrent_unexpired_holder() -> None:
    transition = _bare_transition()
    transition.holder = "this-process"
    lease = {
        "metadata": {"resourceVersion": "9"},
        "spec": {
            "holderIdentity": "another-process",
            "leaseDurationSeconds": 3600,
            "renewTime": "2999-01-01T00:00:00Z",
        },
    }
    transition.guarded_kubectl = FakeCommand(lambda _arguments, _kwargs: _result(stdout=json.dumps(lease)))

    with pytest.raises(TRANSITION.TransitionError, match="holds the Lease"):
        transition._acquire_lock()


def test_lease_renewal_rejects_stale_holder_or_transition_fence() -> None:
    transition = _bare_transition()
    stale = {
        "metadata": {"resourceVersion": "10"},
        "spec": {
            "holderIdentity": "new-holder",
            "leaseTransitions": transition.fence_transitions + 1,
            "leaseDurationSeconds": 60,
            "renewTime": "2999-01-01T00:00:00Z",
        },
    }
    transition.guarded_kubectl = FakeCommand(lambda _arguments, _kwargs: _result(stdout=json.dumps(stale)))

    with pytest.raises(TRANSITION.TransitionError, match="fence is stale"):
        TRANSITION.Transition._renew_fence(transition)


def test_every_policy_patch_has_resource_version_and_renews_fence() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    current = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    events: list[str] = []
    transition._renew_fence = lambda: events.append("renew") or {}
    transition.verify_ready_pods = lambda *_args, **_kwargs: 1

    def api(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        if arguments[:2] == ("get", "networkpolicy"):
            return _result(stdout=json.dumps(current))
        patch = json.loads(arguments[arguments.index("--patch") + 1])
        assert patch["metadata"]["resourceVersion"] == "17"
        if "--dry-run=server" in arguments:
            events.append("dry-run")
            return _result(stdout=json.dumps(current))
        events.append("mutate")
        updated = json.loads(json.dumps(current))
        updated["metadata"].setdefault("annotations", {}).update(patch["metadata"]["annotations"])
        updated["spec"] = patch["spec"]
        return _result(stdout=json.dumps(updated))

    transition.guarded_kubectl = FakeCommand(api)
    transition.patch_guard("public-envoy", candidate.proxy, candidate)

    assert events == ["dry-run", "renew", "mutate"]


@pytest.mark.parametrize("status", ["failed", "pending-install", "pending-upgrade", "pending-rollback"])
def test_release_identity_rejects_failed_or_pending_release(status: str) -> None:
    transition = _bare_transition()

    class Helm:
        @staticmethod
        def run(*_args: str, **_kwargs: Any) -> Any:
            return _result(stdout=json.dumps([{"name": "test-release", "revision": "9", "status": status}]))

    transition.helm = Helm()
    with pytest.raises(TRANSITION.TransitionError, match="not successfully deployed"):
        transition._release_identity(_candidate().proxy, _candidate().controller)


def test_deployed_target_binds_exact_successor_manifest_and_release_uids() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    deployed = {
        **candidate.release,
        "revision": "139",
        "identity_uid": "uid-helm-release-139",
        "storage": {
            "name": "sh.helm.release.v1.test-release.v139",
            "uid": "uid-helm-release-139",
            "resource_version": "1390",
            "revision": "139",
            "status": "deployed",
        },
        "deployed_manifest_sha256": candidate.complete_render_sha256,
    }
    transition._release_identity = lambda *_args, **_kwargs: deployed
    assert transition.verify_deployed_target(candidate) == deployed

    transition._release_identity = lambda *_args, **_kwargs: {**deployed, "revision": "140"}
    with pytest.raises(TRANSITION.TransitionError, match="exact successor"):
        transition.verify_deployed_target(candidate)


def test_release_storage_identity_is_exact_metadata_only() -> None:
    transition = _bare_transition()

    def storage(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert arguments[:3] == ("get", "secret", "sh.helm.release.v1.test-release.v138")
        assert arguments[3:5] == ("--namespace", "fs2-system")
        assert arguments[5] == "-o"
        assert arguments[6].startswith("jsonpath=")
        return _result(
            stdout=("sh.helm.release.v1.test-release.v138\tstorage-uid\t928\thelm\ttest-release\t138\tsuperseded")
        )

    transition.bootstrap_kubectl = FakeCommand(storage)
    assert transition._release_storage_identity("138", "superseded") == {
        "name": "sh.helm.release.v1.test-release.v138",
        "uid": "storage-uid",
        "resource_version": "928",
        "revision": "138",
        "status": "superseded",
    }


def test_completed_receipt_binds_staged_and_post_upgrade_storage_rv_and_values() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    transition._yaml_documents = lambda _text: [{"manifest": "source"}]
    candidate.release["deployed_manifest_sha256"] = TRANSITION.sha256_json([{"manifest": "source"}])

    class Helm:
        @staticmethod
        def run(*arguments: str, **_kwargs: Any) -> Any:
            if arguments[0] == "history":
                return _result(
                    stdout=json.dumps([{"revision": 138, "status": "superseded", "description": "Upgrade complete"}])
                )
            if arguments[:2] == ("get", "manifest"):
                return _result(stdout="manifest")
            if arguments[:2] == ("get", "values"):
                return _result(stdout=json.dumps(DEPLOYED_VALUES))
            raise AssertionError(arguments)

    transition.helm = Helm()
    transition._release_storage_identity = lambda *_args: {
        **candidate.release["storage"],
        "resource_version": "1391",
        "status": "superseded",
    }

    bound = transition.capture_rollback_source(candidate)

    assert bound is not None
    assert bound["staged_storage"]["resource_version"] == "1380"
    assert bound["current_storage"]["resource_version"] == "1391"
    assert bound["target_values_sha256"] == candidate.release["deployed_values_sha256"]
    assert bound["request_debug_enabled"] is False


def test_successful_helm_history_accepts_exact_completed_rollback_description() -> None:
    assert TRANSITION.Transition._successful_history_entry(
        {"revision": 140, "status": "deployed", "description": "Rollback to 138"}
    )
    assert TRANSITION.Transition._successful_history_entry(
        {"revision": 140, "status": "superseded", "description": "Rollback to 138"}
    )
    for entry in (
        {"revision": 140, "status": "pending-rollback", "description": "Rollback to 138"},
        {"revision": 140, "status": "failed", "description": "Rollback to 138"},
        {"revision": 140, "status": "deployed", "description": "Rollback to latest"},
        {"revision": 140, "status": "deployed", "description": "Rollback to 138 failed"},
    ):
        assert not TRANSITION.Transition._successful_history_entry(entry)


def test_release_identity_accepts_successful_helm4_rollback_revision() -> None:
    transition = _bare_transition()
    candidate = _candidate()

    class Helm:
        @staticmethod
        def run(*arguments: str, **_kwargs: Any) -> Any:
            if arguments[0] == "list":
                return _result(stdout=json.dumps([{"name": "test-release", "revision": "140", "status": "deployed"}]))
            if arguments[0] == "history":
                return _result(
                    stdout=json.dumps([{"revision": 140, "status": "deployed", "description": "Rollback to 138"}])
                )
            if arguments[:2] == ("get", "manifest"):
                return _result(stdout="manifest")
            if arguments[:2] == ("get", "values"):
                return _result(stdout=json.dumps(DEPLOYED_VALUES))
            raise AssertionError(arguments)

    transition.helm = Helm()
    transition._yaml_documents = lambda _text: [{"kind": "ConfigMap"}]
    transition._release_storage_identity = lambda _revision, _status: {
        "name": "sh.helm.release.v1.test-release.v140",
        "uid": "storage-uid-140",
        "resource_version": "1400",
        "revision": "140",
        "status": "deployed",
    }
    transition.get_policy = lambda name, namespace: {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "resourceVersion": "22",
        },
        "spec": PROXY_SPEC if name == candidate.proxy["normal_name"] else CONTROLLER_SPEC,
    }

    identity = transition._release_identity(candidate.proxy, candidate.controller)

    assert identity["revision"] == "140"
    assert identity["status"] == "deployed"
    assert identity["description"] == "Rollback to 138"
    assert identity["deployed_values_sha256"] == TRANSITION.sha256_json(DEPLOYED_VALUES)


def test_rollback_rejects_arbitrary_revision_and_debug_enabled_target() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    with pytest.raises(TRANSITION.TransitionError, match="receipt-bound stable source"):
        transition._rollback_target(candidate, "7", transition.staged_rollback_source(candidate))

    transition._yaml_documents = lambda _text: [{"kind": "ConfigMap"}]
    candidate.release["revision"] = "138"
    candidate.release["deployed_manifest_sha256"] = TRANSITION.sha256_json([{"kind": "ConfigMap"}])
    candidate.release["deployed_values_sha256"] = TRANSITION.sha256_json({"config": {"requestDebugEnabled": True}})
    candidate.release["request_debug_enabled"] = True

    class Helm:
        @staticmethod
        def run(*arguments: str, **_kwargs: Any) -> Any:
            if arguments[0] == "history":
                return _result(
                    stdout=json.dumps(
                        [
                            {
                                "revision": 138,
                                "status": "superseded",
                                "description": "Upgrade complete",
                            }
                        ]
                    )
                )
            if arguments[:2] == ("get", "manifest"):
                return _result(stdout="manifest")
            if arguments[:2] == ("get", "values"):
                return _result(stdout=json.dumps({"config": {"requestDebugEnabled": True}}))
            raise AssertionError(arguments)

    transition.helm = Helm()
    transition._release_storage_identity = lambda _revision, _status: candidate.release["storage"]
    with pytest.raises(TRANSITION.TransitionError, match="request debugging is enabled"):
        transition._rollback_target(candidate, "138", transition.staged_rollback_source(candidate))


def test_rollback_requires_exact_staged_values_and_storage_resource_version() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    transition._yaml_documents = lambda _text: [{"manifest": "source"}]
    candidate.release["deployed_manifest_sha256"] = TRANSITION.sha256_json([{"manifest": "source"}])
    bound = transition.staged_rollback_source(candidate)
    bound["current_storage"] = {**bound["current_storage"], "resource_version": "changed"}

    class Helm:
        @staticmethod
        def run(*arguments: str, **_kwargs: Any) -> Any:
            if arguments[0] == "history":
                return _result(
                    stdout=json.dumps([{"revision": 138, "status": "deployed", "description": "Install complete"}])
                )
            raise AssertionError(arguments)

    transition.helm = Helm()
    transition._release_storage_identity = lambda *_args: candidate.release["storage"]
    with pytest.raises(TRANSITION.TransitionError, match="resourceVersion"):
        transition._rollback_target(candidate, "138", bound)

    exact = transition.staged_rollback_source(candidate)
    exact["target_values_sha256"] = "f" * 64
    with pytest.raises(TRANSITION.TransitionError, match="exact staged Helm values"):
        transition._rollback_target(candidate, "138", exact)


def test_live_topology_has_no_caller_rendered_disabled_bypass() -> None:
    source = SCRIPT.read_text()
    terraform = TERRAFORM.read_text()
    assert "public_boundary_enabled" not in source
    assert "internal_rollback" not in source
    assert "public-gateway=false" not in source
    assert "live_topology()" in source
    assert "caller render does not match the protected live boundary topology" in source
    assert "control_plane_network_policy_boundary_applicable" in terraform
    assert 'network_policy_boundary_contract.mode == "public"' in terraform
    assert "count = local.public_edge_enabled ? 1 : 0" not in terraform


def test_live_topology_itself_must_authorize_public_transition() -> None:
    transition = _bare_transition()
    topology = {
        "metadata": {
            "uid": "topology-uid",
            "resourceVersion": "11",
            "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
        },
        "data": {
            "topology.json": json.dumps(
                {
                    "schema": "fs2-serve.nebius.ai/network-policy-boundary-topology/v1",
                    "mode": "internal-only",
                }
            )
        },
    }
    transition.guarded_kubectl = FakeCommand(lambda _arguments, _kwargs: _result(stdout=json.dumps(topology)))

    with pytest.raises(TRANSITION.TransitionError, match="does not authorize a public boundary"):
        transition.live_topology()


def test_pod_listing_is_server_paginated_and_strictly_bounded() -> None:
    transition = _bare_transition()
    calls = 0

    def pages(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        assert arguments[:2] == ("get", "--raw")
        assert "limit=100" in arguments[2]
        return _result(stdout=json.dumps({"metadata": {"continue": f"token-{calls}"}, "items": []}))

    transition.guarded_kubectl = FakeCommand(pages)
    with pytest.raises(TRANSITION.TransitionError, match="exceeded bounded pagination"):
        transition.list_pods_bounded("edge-custom", {"app": "envoy"})
    assert calls == TRANSITION.MAX_POD_PAGES


@pytest.mark.parametrize("failure", ["forbidden", "timeout", "transport closed"])
def test_deny_lookup_fails_closed_on_every_non_not_found_error(failure: str) -> None:
    transition = _bare_transition()

    def failed_get(_arguments: tuple[str, ...], kwargs: dict[str, Any]) -> Any:
        assert kwargs["check"] is False
        return _result(returncode=1, stderr=failure)

    transition.guarded_kubectl = FakeCommand(failed_get)
    with pytest.raises(TRANSITION.TransitionError, match="without a verified NotFound"):
        transition.get_optional_policy("deny", "edge-custom")


def test_deny_lookup_accepts_only_successful_empty_ignore_not_found() -> None:
    transition = _bare_transition()

    def verified_not_found(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert "--ignore-not-found" in arguments
        return _result(stdout="", returncode=0)

    transition.guarded_kubectl = FakeCommand(verified_not_found)
    assert transition.get_optional_policy("deny", "edge-custom") is None


def test_receipt_binds_candidate_and_exact_boundary_uid_rv_spec() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    config_map = {
        "metadata": {
            "name": TRANSITION.RECEIPT_NAME,
            "namespace": "fs2-system",
            "uid": "receipt-uid",
            "resourceVersion": "31",
        },
        "data": {"receipt.json": json.dumps({"schema": TRANSITION.SCHEMA, "phase": "guards-ready"})},
    }
    captured: dict[str, Any] = {}
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )

    def receipt_api(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        if arguments[:2] == ("get", "configmap"):
            return _result(stdout=json.dumps(config_map))
        if arguments[:2] == ("get", "networkpolicy"):
            return _result(stdout=json.dumps(deny))
        patch = json.loads(arguments[arguments.index("--patch") + 1])
        captured.update(json.loads(patch["data"]["receipt.json"]))
        return _result(stdout=json.dumps(config_map))

    transition.guarded_kubectl = FakeCommand(receipt_api)
    transition.write_receipt("staged", candidate, {"public-envoy": proxy, "envoy-controller": controller})

    assert captured["schema"] == TRANSITION.SCHEMA
    assert captured["phase"] == "staged"
    assert captured["candidate"]["candidate_sha256"] == "a" * 64
    assert captured["candidate"]["chart_sha256"] == "b" * 64
    assert captured["candidate"]["values_sha256"] == "c" * 64
    assert captured["candidate"]["complete_render_sha256"] == "e" * 64
    assert captured["candidate"]["release"]["revision"] == "138"
    assert captured["candidate"]["release"]["deployed_values_sha256"] == TRANSITION.sha256_json(DEPLOYED_VALUES)
    assert captured["candidate"]["release"]["storage"]["resource_version"] == "1380"
    assert captured["candidate"]["topology"]["uid"] == "topology-uid"
    assert captured["receipt_object"] == {
        "namespace": "fs2-system",
        "name": TRANSITION.RECEIPT_NAME,
        "uid": "receipt-uid",
        "prior_resource_version": "31",
    }
    proxy_receipt = captured["boundary_objects"]["public-envoy"]
    assert proxy_receipt["uid"] == "uid-public-envoy"
    assert proxy_receipt["resource_version"] == "17"
    assert proxy_receipt["spec"] == PROXY_SPEC
    assert len(proxy_receipt["spec_sha256"]) == 64
    assert captured["boundary_objects"]["default-deny"]["uid"] == "uid-default-deny"


def test_complete_is_idempotent_and_never_deletes_permanent_boundaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    state = {"phase": "staged"}
    deployed_release = {**candidate.release, "revision": "139"}
    rollback_source = {"bound": True}
    transition.render_candidate = lambda: candidate
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: (
        {},
        {
            "phase": state["phase"],
            "candidate": candidate.as_dict(),
            "deployed_release": deployed_release,
            "rollback_source": rollback_source,
        },
    )
    transition.verify_local_candidate = lambda _receipt: candidate
    transition.verify_deployed_target = lambda _candidate: deployed_release
    transition.capture_rollback_source = lambda _candidate: rollback_source
    transition.current_guards = lambda _candidate: {
        "public-envoy": proxy,
        "envoy-controller": controller,
    }
    transition.verify_normal = lambda _role, _policy: None
    transition.verify_deny_active = lambda _candidate: None
    transition.get_policy = lambda _name, _namespace: _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    transition.verify_receipt_boundaries = lambda _receipt, _boundaries: None

    def record(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        state["phase"] = phase
        return {}

    transition.write_receipt = record
    transition.complete()
    transition.complete()

    assert state["phase"] == "active"
    assert capsys.readouterr().out.count("boundaries=permanent") == 2
    source = SCRIPT.read_text()
    assert 'self.guarded.run("delete"' not in source
    assert '"uninstall"' not in source


def test_retry_rejects_replaced_or_modified_boundary_identity() -> None:
    candidate = _candidate()
    boundary = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    recorded = {
        "boundary_objects": {
            "public-envoy": {
                **TRANSITION.Transition._guard_receipt(boundary),
                "resource_version": "16",
            }
        }
    }

    with pytest.raises(TRANSITION.TransitionError, match="durable receipt"):
        TRANSITION.Transition.verify_receipt_boundaries(recorded, {"public-envoy": boundary})


def test_protected_policy_patches_are_server_dry_run_before_mutation() -> None:
    source = SCRIPT.read_text()
    guard = source.split("    def patch_guard(", maxsplit=1)[1].split("    def verify_guard(", maxsplit=1)[0]
    deny = source.split("    def _deny_patch(", maxsplit=1)[1].split("    def activate_deny(", maxsplit=1)[0]
    assert guard.index('"--dry-run=server"') < guard.index("result = self.guarded.run")
    assert deny.index('"--dry-run=server"') < deny.index("result = self.guarded.run")


def test_rollback_failure_leaves_deny_relaxed_and_durable_prepared_receipt() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    state: dict[str, Any] = {"phase": "staged", "manifest": "current", "deny": "active", "extra": {}}
    writes: list[str] = []
    transition.render_candidate = lambda: candidate
    transition._target_manifest = lambda _revision: "target"
    transition._current_manifest = lambda: state["manifest"]
    transition.lock = contextlib.nullcontext
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    transition.receipt = lambda: (
        {},
        {
            "phase": state["phase"],
            "candidate": candidate.as_dict(),
            "boundary_objects": {
                "public-envoy": TRANSITION.Transition._guard_receipt(proxy),
                "envoy-controller": TRANSITION.Transition._guard_receipt(controller),
                "default-deny": TRANSITION.Transition._guard_receipt(deny),
            },
            **state["extra"],
        },
    )

    def get_policy(name: str, _namespace: str) -> dict[str, Any]:
        if name == candidate.proxy["guard_name"]:
            return proxy
        if name == candidate.controller["guard_name"]:
            return controller
        return deny

    transition.get_policy = get_policy
    transition.get_optional_policy = lambda _name, _namespace: deny
    transition.verify_live_topology = lambda _candidate: None
    target_hash = TRANSITION.sha256_json([{"manifest": "target"}])
    transition._rollback_target = lambda _candidate, _revision, _bound_source: {
        "target_revision": "7",
        "target_manifest_sha256": target_hash,
        "target_values_sha256": "f" * 64,
        "request_debug_enabled": False,
    }
    transition._release_identity = lambda *_args, **_kwargs: {**candidate.release, "revision": "139"}
    transition._yaml_documents = lambda text: [{"manifest": text}]
    transition._revalidate_rollback_target = lambda *_args: None

    def relax(_candidate: Any) -> str:
        state["deny"] = "relaxed"
        return "relaxed-zero-selected-pods"

    transition.relax_deny = relax

    def write(phase: str, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        writes.append(phase)
        state["phase"] = phase
        state["extra"] = kwargs.get("extra", {})
        return {}

    transition.write_receipt = write

    transition.run_fenced_helm = lambda *_args: (_ for _ in ()).throw(
        TRANSITION.TransitionError("injected rollback failure")
    )
    with pytest.raises(TRANSITION.TransitionError, match="injected rollback failure"):
        transition.rollback()

    assert state["deny"] == "relaxed"
    assert writes == ["rollback-relaxing", "rollback-prepared"]


def test_destroy_resumes_after_deny_mutation_before_commit_receipt() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    proxy = _boundary("public-envoy", candidate.proxy["guard_name"], candidate.proxy["namespace"], PROXY_SPEC)
    controller = _boundary(
        "envoy-controller",
        candidate.controller["guard_name"],
        candidate.controller["namespace"],
        CONTROLLER_SPEC,
    )
    old_deny = _boundary(
        "default-deny",
        candidate.deny_name,
        candidate.proxy["namespace"],
        {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    )
    live_deny = json.loads(json.dumps(old_deny))
    live_deny["metadata"]["resourceVersion"] = "18"
    live_deny["spec"] = {
        "podSelector": {"matchLabels": TRANSITION.RELAXED_SELECTOR},
        "policyTypes": ["Ingress"],
        "ingress": [],
    }
    prior = {
        "phase": "destroy-relaxing",
        "candidate": candidate.as_dict(),
        "boundary_objects": {
            "public-envoy": TRANSITION.Transition._guard_receipt(proxy),
            "envoy-controller": TRANSITION.Transition._guard_receipt(controller),
            "default-deny": TRANSITION.Transition._guard_receipt(old_deny),
        },
    }
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: ({}, prior)
    transition.verify_live_topology = lambda *_args: None
    transition.boundary_objects = lambda _candidate: (
        {"public-envoy": proxy, "envoy-controller": controller},
        live_deny,
    )
    events: list[str] = []
    transition.relax_deny = lambda _candidate: events.append("relax-idempotent") or "relaxed-zero-selected-pods"
    transition.write_receipt = lambda phase, *_args, **_kwargs: events.append(f"receipt:{phase}") or {}

    transition.destroy()

    assert events == ["relax-idempotent", "receipt:destroy-prepared"]


def test_signed_recovery_is_reversible_audit_warn_or_deny_and_never_delete() -> None:
    transition = _bare_transition()
    transition.arguments.recovery_reference = "SEC-1234"
    transition._recovery_approval = lambda: {"signed": {"approved": True}, "signature": "A" * 86}
    transition.live_topology = lambda: {
        "uid": "topology-uid-000000000000",
        "resource_version": "16",
        "sha256": "a" * 64,
    }
    transition.receipt = lambda: (
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "namespace": "fs2-system",
                "name": "fs2-network-policy-transition",
                "uid": "receipt-uid-0000000000000",
                "resourceVersion": "17",
                "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
            },
            "data": {"receipt.json": TRANSITION.canonical({"phase": "active"})},
        },
        {"phase": "active"},
    )
    transition._lease = lambda: {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "namespace": "fs2-system",
            "name": "fs2-network-policy-transition",
            "uid": "lease-uid-000000000000000",
            "resourceVersion": "18",
            "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
        },
        "spec": {"holderIdentity": "test-holder", "leaseTransitions": 3},
    }

    class Handoff:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def request(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((action, body))
            actions = ["Audit", "Warn"] if body["mode"] == "audit-warn" else ["Deny"]
            binding = {
                    "apiVersion": "admissionregistration.k8s.io/v1",
                    "kind": "ValidatingAdmissionPolicyBinding",
                    "metadata": {
                        "name": "fs2-network-policy-boundary",
                        "uid": "binding-uid-000000000000",
                        "resourceVersion": "31",
                        "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
                    },
                    "spec": {
                        "policyName": "fs2-network-policy-boundary",
                        "validationActions": actions,
                    },
                }
            parameter = {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "namespace": "fs2-system",
                        "name": "fs2-network-policy-boundary-parameters",
                        "uid": "parameter-uid-0000000000",
                        "resourceVersion": "32",
                        "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
                    },
                    "data": {"mode": body["mode"], "delete_allowed": "false"},
                }

            def evidence(resource: dict[str, Any]) -> dict[str, Any]:
                metadata = resource["metadata"]
                state = {
                    "labels": metadata.get("labels", {}),
                    "annotations": metadata.get("annotations", {}),
                    "spec": resource.get("spec"),
                    "data": resource.get("data"),
                }
                return {
                    "api_version": resource["apiVersion"],
                    "kind": resource["kind"],
                    "namespace": metadata.get("namespace", ""),
                    "name": metadata["name"],
                    "uid": metadata["uid"],
                    "resource_version": metadata["resourceVersion"],
                    "labels": state["labels"],
                    "annotations": state["annotations"],
                    "spec": state["spec"],
                    "data": state["data"],
                    "state_sha256": TRANSITION.sha256_json(state),
                }

            return {
                "binding": binding,
                "parameter": parameter,
                "receipt": {
                    "metadata": {
                        "uid": "receipt-uid-0000000000000",
                        "resourceVersion": "33",
                    },
                    "data": {
                        "receipt.json": TRANSITION.canonical(
                            {
                                "phase": "active",
                                "security_recovery": {
                                    "state": "complete",
                                    "mode": body["mode"],
                                    "recovery_reference": body["recovery_reference"],
                                    "after": {
                                        "binding": evidence(binding),
                                        "parameter": evidence(parameter),
                                    },
                                },
                            }
                        )
                    },
                },
            }

    handoff = Handoff()
    transition.security_handoff = handoff
    transition.recover_admission("audit-warn")
    transition.recover_admission("deny")

    assert [body["mode"] for _action, body in handoff.calls] == ["audit-warn", "deny"]
    assert all(action == "set-admission-recovery" for action, _body in handoff.calls)
    assert all(body["delete_allowed"] is False for _action, body in handoff.calls)
    assert all(body["lease"]["holder_identity"] == "test-holder" for _action, body in handoff.calls)
    assert all(body["approval"]["signature"] == "A" * 86 for _action, body in handoff.calls)


def test_foundation_security_owner_permanently_owns_boundary_outside_workloads() -> None:
    boundary = BOUNDARY_TERRAFORM.read_text()
    preflight = BOUNDARY_PREFLIGHT.read_text()
    inventory_schema = json.loads(SUBJECT_INVENTORY_SCHEMA.read_text())
    provider_schema = json.loads(PROVIDER_SNAPSHOT_SCHEMA.read_text())
    control_plane = TERRAFORM.read_text()

    for resource in (
        'resource "kubernetes_network_policy_v1" "control_plane_public_envoy_boundary"',
        'resource "kubernetes_network_policy_v1" "control_plane_envoy_controller_boundary"',
        'resource "kubernetes_network_policy_v1" "control_plane_envoy_default_deny"',
        'resource "kubernetes_manifest" "control_plane_network_policy_transition_lease"',
        'resource "kubernetes_config_map_v1" "control_plane_network_policy_transition_receipt"',
        'resource "kubernetes_config_map_v1" "control_plane_network_policy_topology"',
        'resource "kubernetes_config_map_v1" "control_plane_network_policy_boundary_parameters"',
        'resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission"',
        'resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_owner"',
    ):
        assert resource in boundary
        assert resource not in control_plane
    assert '"fs2.nebius.ai/network-policy-boundary" = "permanent"' in boundary
    assert "request.userInfo.username in [" in boundary
    assert "'${local.control_plane_network_policy_security_bootstrap}'" in boundary
    assert 'resource "kubernetes_service_account_v1" "control_plane_network_policy_transition"' in boundary
    assert "automount_service_account_token = false" in boundary
    assert 'kind      = "ServiceAccount"' not in boundary
    assert "decommission-receipt-sha256" not in boundary
    assert "permanent boundary objects are never deleted" in boundary
    assert boundary.count("prevent_destroy = true") >= 21
    assert boundary.count("provider = kubernetes.network_policy_security_owner") >= 28
    assert 'ordinary_named_can "$verb" "$resource" fs2-network-policy-boundary' in boundary
    assert 'ordinary_can deletecollection "$resource"' in boundary
    assert 'security_can deletecollection "$resource"' in boundary
    assert 'security_named_can delete "$resource" fs2-network-policy-boundary' in boundary
    assert 'security_named_can delete "$resource" fs2-network-policy-boundary)" = "no"' in boundary
    assert 'ordinary_named_can "$verb" "$resource" "$name" --namespace "$namespace"' in boundary
    assert "fs2-network-policy-boundary-parameters" in boundary
    assert "ordinary_named_can create serviceaccounts fs2-network-policy-transition --subresource=token" in boundary
    assert 'ordinary_can impersonate "$resource"' in boundary
    assert 'ordinary_named_can impersonate "$resource" "$name"' in boundary
    assert 'security_named_can impersonate "$resource" "$name"' in boundary
    assert 'bootstrap_named_can impersonate "$resource" "$name"' in boundary
    for target in (
        "users|$FS2_SECURITY_OWNER_USERNAME",
        "groups|system:masters",
        "groups|system:authenticated",
        "groups|system:serviceaccounts",
        "users|fs2-network-policy-security-probe",
        "serviceaccounts|fs2-system:fs2-network-policy-security-probe",
        "uids.authentication.k8s.io|00000000-0000-4000-8000-000000000000",
        "userextras.authentication.k8s.io|scopes",
        "userextras.authentication.k8s.io|fs2.nebius.ai/security-probe",
    ):
        assert target in boundary
    assert 'ordinary_can "$verb" "$resource" "$${namespace_args[@]}"' in boundary
    assert 'ordinary_named_can "$verb" "$resource" "$name"' in boundary
    assert "security_named_can escalate" in boundary
    assert "get namespace kube-system -o 'jsonpath={.metadata.uid}'" in boundary
    assert 'test "$ordinary_server" = "$security_server"' in boundary
    assert "auth whoami -o json" in boundary
    assert "normalize_identity" in boundary
    assert "security_bootstrap_kubeconfig" in boundary
    assert 'security_can "$verb" "$resource")" = "no"' in boundary
    assert 'ordinary_named_can update namespaces/finalize fs2-system' in boundary
    assert 'security_named_can update namespaces/finalize fs2-system' in boundary
    assert 'test "$(id -g)" = "$FS2_PEER_GID"' in boundary
    assert "SubjectAccessReview" in boundary
    assert "FS2_SECURITY_INVENTORY_SUBJECTS" in boundary
    assert "security_subject_inventory" in boundary
    assert '"fs2-serve.nebius.ai/network-policy-identity-boundary/v3"' in boundary
    assert 'socket_directory_contract  = "precreated-setgid-02710"' in boundary
    assert 'test "$(stat -c \'%a\' "$socket_parent")" = "2710"' in boundary
    assert "FS2_MINIMUM_ROLLBACK_SECONDS" in boundary
    assert "network-policy-credential-set-sha256" in boundary
    assert "same-epoch updates must preserve exact data" in boundary
    assert "subject_denied" in boundary
    assert "credential_expiry_epoch" in boundary
    assert 'data "external" "control_plane_network_policy_security_preflight_v2"' in boundary
    assert "plan_preflight_sha256" in boundary
    assert "SubjectAccessReview" in preflight
    assert "credential_expiry" in preflight
    assert "verified_subject_inventory" in preflight
    assert "Ed25519PublicKey" in preflight
    assert '("", "serviceaccounts")' in preflight
    assert '"uids.authentication.k8s.io"' in preflight
    assert '"certificatesigningrequests.certificates.k8s.io"' in preflight
    assert '"signers.certificates.k8s.io"' in preflight
    assert "kubernetes_subject_inventory" in preflight
    assert "paginated_collection" in preflight
    assert "provider_snapshot_sha256" in preflight
    assert '"--all-namespaces"' in preflight
    assert "--resource-name=fs2-network-policy-boundary" in preflight
    assert 'subresource="finalize"' in preflight
    assert "stat -c '%u' \"$FS2_SECURITY_KUBECONFIG\"" in boundary
    assert 'test "$(id -u)" != "$FS2_PEER_UID"' in boundary
    assert 'security_named_can "$verb" "$resource" fs2-network-policy-boundary' in boundary
    assert 'operations  = ["UPDATE", "DELETE"]' in boundary
    assert 'resources   = ["namespaces/finalize"]' in boundary
    assert "request.userInfo.username in [" in boundary
    assert 'validationActions = ["Deny"]' in boundary
    assert '["Audit", "Warn", "Deny"]' in boundary
    assert re.search(r"delete_allowed\s+= false", boundary)
    assert inventory_schema["properties"]["signed"]["properties"]["cluster"]["additionalProperties"] is False
    assert inventory_schema["properties"]["signature"]["pattern"] == "^[A-Za-z0-9_-]{86}$"
    assert provider_schema["properties"]["signed"]["properties"]["complete"] == {"const": True}
    assert provider_schema["properties"]["signed"]["properties"]["pagination"]["additionalProperties"] is False
    assert 'verbs          = ["get", "patch", "update", "delete"]' not in boundary
    assert "resource_names = [" in boundary
    assert 'resources  = ["pods"]' not in boundary
    auditor = boundary.split(
        'resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_auditor"',
        maxsplit=1,
    )[1].split('resource "kubernetes_cluster_role_binding_v1"', maxsplit=1)[0]
    assert 'resources  = ["namespaces", "serviceaccounts"]' in auditor
    assert 'resources  = ["roles", "clusterroles"]' in auditor
    assert 'resources  = ["rolebindings", "clusterrolebindings"]' in auditor
    assert 'verbs      = ["list"]' in auditor
    assert 'resources      = ["clusterrolebindings"]' in auditor
    assert 'verbs          = ["get"]' in auditor
    assert 'resources  = ["certificatesigningrequests"]' in auditor
    assert 'resources  = ["pods"]' not in auditor
    assert 'resources      = ["networkpolicies"]' in boundary
    assert 'resources      = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]' in boundary
    assert 'kind      = "User"' in boundary
    deny_resource = boundary.split(
        'resource "kubernetes_network_policy_v1" "control_plane_envoy_default_deny"',
        maxsplit=1,
    )[1].split('resource "kubernetes_role_v1"', maxsplit=1)[0]
    assert "kubernetes_network_policy_v1.control_plane_public_envoy_boundary" in deny_resource
    assert "kubernetes_network_policy_v1.control_plane_envoy_controller_boundary" in deny_resource

    destroy = control_plane.index("when        = destroy")
    destroy_action = control_plane.index('"$FS2_TRANSITION_SCRIPT" destroy', destroy)
    helm_dependency = control_plane.index("terraform_data.control_plane_network_policy_transition_stage")
    complete_dependency = control_plane.rindex("depends_on = [helm_release.control_plane]")
    assert destroy < destroy_action
    assert helm_dependency < complete_dependency
    assert "atomic          = false" in control_plane
    assert "cleanup_on_fail = false" in control_plane
    assert "security_owner_kubeconfig" not in control_plane
    assert "--security-handoff-socket" in control_plane
    assert "--security-handoff-server-public-key" in control_plane
    assert "--security-handoff-client-public-key" in control_plane
    assert "--security-handoff-client-private-key" in control_plane
    assert "stages/workloads/control_plane_network_policy_boundary.tf" not in control_plane


def test_security_subject_inventory_is_signed_complete_cluster_bound_and_rollback_valid() -> None:
    recovery_private_key = Ed25519PrivateKey.generate()
    provider_private_key = Ed25519PrivateKey.generate()
    recovery_public_value = (
        TRANSITION.base64.urlsafe_b64encode(recovery_private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    provider_public_value = (
        TRANSITION.base64.urlsafe_b64encode(provider_private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    now = TRANSITION.dt.datetime.now(TRANSITION.dt.UTC)
    cluster = ("https://reviewed-api.example.invalid", "kube-system-uid-000000000000")
    provider_users = [{
        "provider_subject_id": "tenantuseraccount-reviewer-001",
        "username": "reviewer@example.invalid",
        "groups": ["fs2-reviewers"],
    }]
    users = [{"username": "reviewer@example.invalid", "groups": ["fs2-reviewers"]}]
    groups = ["fs2-platform-admins"]
    adapter_sha256 = PREFLIGHT.hashlib.sha256(PROVIDER_ADAPTER.read_bytes()).hexdigest()
    trust_anchor_sha256 = "d" * 64
    principal_id = "serviceaccount-directory-reader-001"
    expected_permits = [{
        "parent_id": principal_id,
        "parent_kind": "service-account",
        "resource_id": "tenant-example0001",
        "role": "auditor",
    }]
    provider_trust = {
        "provider": "nebius-iam",
        "adapter": {"id": PREFLIGHT.PROVIDER_ADAPTER_ID, "sha256": adapter_sha256},
        "directory_execution": {
            "principal_type": "service-account",
            "principal_id": principal_id,
            "credential_sha256": "e" * 64,
            "api_endpoint_sha256": "f" * 64,
            "provider_issuer_sha256": "0" * 64,
            "directory_reader_access": {
                "approved_role": "auditor",
                "approved_role_effect": "view-metadata-without-data-or-mutation",
                "expected_permits": expected_permits,
                "expected_permits_sha256": PREFLIGHT.hashlib.sha256(
                    PREFLIGHT.canonical(expected_permits).encode()
                ).hexdigest(),
            },
        },
        "directory_query": {
            "tenant_id": "tenant-example0001",
            "page_size": 200,
            "max_pages": 20,
            "max_records": 100,
            "snapshot_ttl_seconds": 10800,
            "consistency_passes": 2,
        },
        "execution_sha256": "1" * 64,
        "kubernetes_authentication_sha256": "2" * 64,
        "tenant_sha256": "a" * 64,
        "query_sha256": "b" * 64,
        "valid_from": (now - TRANSITION.dt.timedelta(hours=1)).isoformat(),
        "expires_at": (now + TRANSITION.dt.timedelta(hours=4)).isoformat(),
    }
    provider_authority = {
        "snapshot_public_key": provider_public_value,
        "snapshot_signer_key_id": PREFLIGHT.hashlib.sha256(provider_public_value.encode()).hexdigest(),
        "valid_from": (now - TRANSITION.dt.timedelta(hours=1)).isoformat(),
        "expires_at": (now + TRANSITION.dt.timedelta(hours=4)).isoformat(),
    }
    raw_page = {
        "operation": "tenant-user-account-with-attributes.list",
        "request_token": "",
        "response": {"items": [{"id": "tenantuseraccount-reviewer-001"}], "next_page_token": ""},
        "next_token": "",
    }
    collection_sha256 = PREFLIGHT.hashlib.sha256(PREFLIGHT.canonical([raw_page]).encode()).hexdigest()
    page_receipt = {
        "index": 0,
        "request_cursor_sha256": PREFLIGHT.hashlib.sha256(b"").hexdigest(),
        "response_sha256": PREFLIGHT.hashlib.sha256(
            PREFLIGHT.canonical(
                {"operation": raw_page["operation"], "response": raw_page["response"]}
            ).encode()
        ).hexdigest(),
        "next_cursor_sha256": "",
    }
    membership_page = {
        "operation": (
            "group-membership.list-member-of:"
            f"{PREFLIGHT.hashlib.sha256(principal_id.encode()).hexdigest()}"
        ),
        "request_token": "",
        "response": {"items": [], "next_page_token": ""},
        "next_token": "",
    }
    permit_operation = f"access-permit.list:{PREFLIGHT.hashlib.sha256(principal_id.encode()).hexdigest()}"
    access_permit_page = {
        "operation": permit_operation,
        "request_token": "",
        "response": {
            "items": [{
                "metadata": {
                    "id": "accesspermit-directory-reader-001",
                    "parent_id": principal_id,
                },
                "spec": {"resource_id": "tenant-example0001", "role": "auditor"},
            }],
            "next_page_token": "",
        },
        "next_token": "",
    }
    authorization_evidence = {
        "whoami": {
            "subject": {"type": "service-account", "id": principal_id},
            "tenant_id": "tenant-example0001",
        },
        "membership_pages": [membership_page],
        "principal_group_ids": [],
        "subject_permit_pages": [{
            "subject_id": principal_id,
            "subject_kind": "service-account",
            "pages": [access_permit_page],
        }],
        "effective_permits": [{
            "permit_id": "accesspermit-directory-reader-001",
            **expected_permits[0],
        }],
        "effective_roles": ["auditor"],
        "access_contract_sha256": provider_trust["directory_execution"][
            "directory_reader_access"
        ]["expected_permits_sha256"],
        "page_count": 2,
        "record_count": 1,
    }
    authorization_sha256 = PREFLIGHT.hashlib.sha256(
        PREFLIGHT.canonical(authorization_evidence).encode()
    ).hexdigest()
    cycle_material = {
        "mode": "authorization-directory-directory-authorization",
        "authorization_before_sha256": authorization_sha256,
        "directory_collection_sha256": collection_sha256,
        "authorization_after_sha256": authorization_sha256,
    }
    provider_authorization = {
        "cycle": {
            **cycle_material,
            "cycle_sha256": PREFLIGHT.hashlib.sha256(
                PREFLIGHT.canonical(cycle_material).encode()
            ).hexdigest(),
        },
        "collections": [
            {
                "index": index,
                "phase": phase,
                "evidence": authorization_evidence,
                "sha256": authorization_sha256,
            }
            for index, phase in enumerate(("before-directory", "after-directory"))
        ],
    }
    provider_signed = {
        "schema": "fs2-serve.nebius.ai/security-subject-provider-snapshot/v3",
        "snapshot_id": "provider-snapshot-epoch-001",
        "provider": "nebius-iam",
        "adapter": {"id": PREFLIGHT.PROVIDER_ADAPTER_ID, "sha256": adapter_sha256},
        "trust_anchor_sha256": trust_anchor_sha256,
        "provider_collection_authority_sha256": "4" * 64,
        "execution_sha256": "1" * 64,
        "kubernetes_authentication_sha256": "2" * 64,
        "provider_principal": {
            "type": "service-account",
            "id": "serviceaccount-directory-reader-001",
            "credential_sha256": "e" * 64,
        },
        "api_endpoint_sha256": "f" * 64,
        "provider_issuer_sha256": "0" * 64,
        "tenant_sha256": "a" * 64,
        "query_sha256": "b" * 64,
        "provider_authorization": provider_authorization,
        "complete": True,
        "pagination": {
            "page_size": 200,
            "subject_count": 2,
            "consistency": {
                "mode": "double-collect-byte-identical",
                "passes": 2,
                "collection_sha256": collection_sha256,
            },
            "collections": [
                {
                    "index": index,
                    "page_count": 1,
                    "record_count": 2,
                    "terminal_cursor": "",
                    "pages": [page_receipt],
                    "sha256": collection_sha256,
                }
                for index in range(2)
            ],
        },
        "raw_collections": [[raw_page], [raw_page]],
        "human_users": provider_users,
        "human_groups": groups,
        "captured_at": (now - TRANSITION.dt.timedelta(minutes=1)).isoformat(),
        "expires_at": (
            now - TRANSITION.dt.timedelta(minutes=1) + TRANSITION.dt.timedelta(seconds=10800)
        ).isoformat(),
        "signer_key_id": PREFLIGHT.hashlib.sha256(provider_public_value.encode()).hexdigest(),
    }
    provider_signature = TRANSITION.base64.urlsafe_b64encode(
        provider_private_key.sign(PREFLIGHT.canonical(provider_signed).encode())
    ).decode().rstrip("=")
    provider_envelope = PREFLIGHT.canonical({"signed": provider_signed, "signature": provider_signature})
    Draft202012Validator(json.loads(PROVIDER_SNAPSHOT_SCHEMA.read_text())).validate(
        json.loads(provider_envelope)
    )
    verified_provider, provider_users, provider_groups, provider_hash = PREFLIGHT.verified_provider_snapshot(
        provider_envelope,
        provider_trust,
        provider_authority=provider_authority,
        provider_authority_sha256="4" * 64,
        authoritative_capture=provider_signed,
        trust_anchor_sha256=trust_anchor_sha256,
        adapter_sha256=adapter_sha256,
        required_valid_until=int((now + TRANSITION.dt.timedelta(hours=2)).timestamp()),
        forbidden_usernames=set(),
    )
    mismatched_directory_cycle = {
        **provider_authorization,
        "cycle": {
            **provider_authorization["cycle"],
            "directory_collection_sha256": "0" * 64,
        },
    }
    with pytest.raises(PREFLIGHT.PreflightError, match="cycle receipt"):
        PREFLIGHT.verified_provider_authorization(
            mismatched_directory_cycle,
            provider_trust,
            directory_collection_sha256=collection_sha256,
        )
    signed = {
        "schema": "fs2-serve.nebius.ai/security-subject-inventory/v3",
        "inventory_id": "security-inventory-epoch-001",
        "cluster": {
            "api_server_sha256": PREFLIGHT.hashlib.sha256(cluster[0].encode()).hexdigest(),
            "kube_system_uid": cluster[1],
        },
        "provider_snapshot": {
            "sha256": provider_hash,
            "snapshot_id": provider_signed["snapshot_id"],
            "provider": provider_signed["provider"],
            "adapter": provider_signed["adapter"],
            "trust_anchor_sha256": provider_signed["trust_anchor_sha256"],
            "provider_collection_authority_sha256": provider_signed[
                "provider_collection_authority_sha256"
            ],
            "execution_sha256": provider_signed["execution_sha256"],
            "kubernetes_authentication_sha256": provider_signed[
                "kubernetes_authentication_sha256"
            ],
            "provider_principal": provider_signed["provider_principal"],
            "provider_authorization_sha256": PREFLIGHT.hashlib.sha256(
                PREFLIGHT.canonical(provider_signed["provider_authorization"]).encode()
            ).hexdigest(),
            "api_endpoint_sha256": provider_signed["api_endpoint_sha256"],
            "provider_issuer_sha256": provider_signed["provider_issuer_sha256"],
            "tenant_sha256": provider_signed["tenant_sha256"],
            "query_sha256": provider_signed["query_sha256"],
            "collection_count": 2,
            "collection_sha256": collection_sha256,
            "record_count": 2,
            "captured_at": provider_signed["captured_at"],
            "expires_at": provider_signed["expires_at"],
            "signer_key_id": provider_signed["signer_key_id"],
        },
        "human_users": users,
        "human_groups": groups,
        "issued_at": (now - TRANSITION.dt.timedelta(minutes=1)).isoformat(),
        "expires_at": (now + TRANSITION.dt.timedelta(hours=3)).isoformat(),
        "signer_key_id": PREFLIGHT.hashlib.sha256(recovery_public_value.encode()).hexdigest(),
    }
    signature = TRANSITION.base64.urlsafe_b64encode(
        recovery_private_key.sign(PREFLIGHT.canonical(signed).encode())
    ).decode().rstrip("=")
    envelope = PREFLIGHT.canonical({"signed": signed, "signature": signature})
    Draft202012Validator(json.loads(SUBJECT_INVENTORY_SCHEMA.read_text())).validate(
        json.loads(envelope)
    )

    subjects, inventory_hash = PREFLIGHT.verified_subject_inventory(
        envelope,
        recovery_public_value,
        cluster=cluster,
        rollback_valid_until=int((now + TRANSITION.dt.timedelta(hours=2)).timestamp()),
        forbidden_usernames={"fs2-network-policy-release"},
        provider_signed=verified_provider,
        provider_users=provider_users,
        provider_groups=provider_groups,
        provider_snapshot_sha256=provider_hash,
    )

    assert subjects[0]["username"] == "reviewer@example.invalid"
    assert subjects[1]["groups"] == ["fs2-platform-admins"]
    assert inventory_hash == PREFLIGHT.hashlib.sha256(PREFLIGHT.canonical(signed).encode()).hexdigest()
    tampered = PREFLIGHT.canonical({"signed": {**signed, "human_groups": ["unreviewed"]}, "signature": signature})
    with pytest.raises(PREFLIGHT.PreflightError, match="signature"):
        PREFLIGHT.verified_subject_inventory(
            tampered,
            recovery_public_value,
            cluster=cluster,
            rollback_valid_until=int((now + TRANSITION.dt.timedelta(hours=2)).timestamp()),
            forbidden_usernames=set(),
            provider_signed=verified_provider,
            provider_users=provider_users,
            provider_groups=provider_groups,
            provider_snapshot_sha256=provider_hash,
        )


def test_epoch_authority_provider_provenance_and_delegation_proof_are_structural() -> None:
    boundary = BOUNDARY_TERRAFORM.read_text()
    variables = (SOLUTION_ROOT / "stages" / "foundation" / "variables.tf").read_text()
    workloads = (SOLUTION_ROOT / "stages" / "workloads" / "cluster_contract.tf").read_text()
    preflight = BOUNDARY_PREFLIGHT.read_text()
    retirement = EPOCH_RETIREMENT.read_text()
    provider_adapter = PROVIDER_ADAPTER.read_text()
    transition = SCRIPT.read_text()
    enforcer = ENFORCER_SCRIPT.read_text()

    for source in (boundary, variables, workloads, preflight, transition, enforcer):
        assert "network-policy-identity-boundary/v3" in source or source is variables or source is preflight
    assert "prior_identity_epoch" in variables
    assert "successor_identity_epoch" in variables
    assert "prior_security_owner_kubeconfig_path" in variables
    assert "prior_security_bootstrap_kubeconfig_path" in variables
    assert '"fs2-np-security-owner-${local.control_plane_network_policy_identity_suffix}"' in boundary
    assert 'name      = local.control_plane_network_policy_successor_bootstrap' in boundary
    assert boundary.count("name      = local.control_plane_network_policy_successor_bootstrap") >= 5
    assert boundary.count("name      = local.control_plane_network_policy_successor_owner") == 4
    assert "request.userInfo.username in [" in boundary
    assert "'${local.control_plane_network_policy_successor_owner}'" not in boundary
    assert "'${local.control_plane_network_policy_successor_bootstrap}'" in boundary
    assert "fresh_preapply_prior_owner_authorized" in boundary
    assert "prior_bootstrap_protected_mutation_denied" in boundary
    assert 'allowed_plan_phases               = ["preapply", "resume", "postapply"]' in boundary
    assert 'mutation_binding_states           = ["before", "target"]' in boundary
    assert "postapply_retirement_required" in boundary
    assert "preapply_retired_subjects" in preflight
    assert "*effective_rbac_subjects" in preflight
    assert "rbac_binding_authorization_subjects" in preflight
    assert 'data "external" "control_plane_network_policy_epoch_retirement"' in boundary
    assert "prior_owner_kubeconfig" in retirement
    assert 'can_i(retired, query["context"], "no"' in retirement
    assert "kubernetes_manifest.control_plane_network_policy_boundary_admission_binding" in boundary
    assert "kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_owner" in boundary
    assert "epoch_retirement" in workloads
    assert 'len({str(path) for path in paths.values()}) != 5' in preflight
    assert 'credential_expiry(paths[role], query["context"])' in preflight
    assert "prior live whoami tuples" in preflight

    assert '"users",\n                "groups",\n                "serviceaccounts",' in preflight
    assert '"uids.authentication.k8s.io"' in preflight
    assert '"userextras.authentication.k8s.io"' in preflight
    assert "users.authentication.k8s.io" not in preflight
    assert "groups.authentication.k8s.io" not in preflight
    assert "serviceaccounts.authentication.k8s.io" not in preflight
    assert "certificatesigningrequests.certificates.k8s.io" in preflight
    assert "signers.certificates.k8s.io" in preflight
    assert "for namespace, accounts in service_accounts.items()" in preflight
    assert '("serviceaccounts", account["name"], namespace)' in preflight
    assert "live_identity_uids" in preflight
    assert "live_identity_groups" in preflight
    assert "live_identity_extra_keys" in preflight
    assert "delegation_targets" in preflight
    assert '"/apis/rbac.authorization.k8s.io/v1/clusterroles"' in preflight
    assert '"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings"' in preflight
    assert 'f"/apis/rbac.authorization.k8s.io/v1/namespaces/' in preflight
    assert '}/rolebindings"' in preflight
    assert "paginated_collection" in preflight
    assert "Kubernetes subject inventory drifted" in preflight
    assert 'tuple(("users", user["username"], "") for user in provider_users)' in preflight
    assert 'for group in sorted(' in preflight

    assert "security-subject-provider-snapshot/v3" in preflight
    assert "security_subject_provider_public_key" not in variables
    assert "security_subject_provider_tenant_sha256" not in variables
    assert "security_subject_provider_query_sha256" not in variables
    assert 'Path("/etc/fs2/security/network-policy-provider-trust-anchor-v3.json")' in preflight
    assert "path is not source-fixed" in preflight
    assert "trust_metadata.st_uid != 0" in preflight
    assert "trust_metadata.st_gid != 0" in preflight
    assert "adapter_path.resolve() != expected_adapter" in preflight
    assert "PROVIDER_ADAPTER_ID" in provider_adapter
    assert "tenant-user-account-with-attributes" in provider_adapter
    assert "group-membership" in provider_adapter
    assert '"consistency_passes") != 2' in provider_adapter
    assert "provider directory changed across the required repeat-stability fence" in provider_adapter
    assert "provider collection authority is not independently pinned" in provider_adapter
    assert "provider whoami does not match the parsed credential principal" in provider_adapter
    assert '"iam", "access-permit", "list", "--parent-id", subject_id' in provider_adapter
    assert '"iam", "access-binding"' not in provider_adapter
    assert '"iam", "role", "get"' not in provider_adapter
    assert "provider effective access permits differ from the approved read-only set" in provider_adapter
    assert "provider authorization changed across the directory collection fence" in provider_adapter
    assert "authorization-directory-directory-authorization" in provider_adapter
    assert "verified_provider_authorization" in preflight
    assert "provider principal membership closure is duplicated or incomplete" in preflight
    assert "provider effective access permits differ from the approved read-only set" in preflight
    assert "provider authorization-directory cycle is not byte-stable" in preflight
    assert '* (4 * provider_trust["directory_query"]["max_pages"] + 2)' in preflight
    assert "parsed profile-to-credential binding is not exact" in preflight
    assert "execute_authoritative_provider_adapter" in preflight
    assert "freshly executed authoritative collection" in preflight
    assert "raw transcript digest is not recomputable" in preflight
    assert "verified_cluster_oidc_mapping" in preflight
    assert '"/apis/authentication.k8s.io/v1/tokenreviews"' in preflight
    assert '"--endpoint", execution["api_endpoint"]' in provider_adapter
    assert '"HOME": "/var/empty"' in provider_adapter
    assert '"NEBIUS_CONFIG": str(NEBIUS_CONFIG_PATH)' in provider_adapter
    assert "credential_sha256" in provider_adapter
    assert "provider enumeration exceeded its page bound" in provider_adapter
    assert "json.load(sys.stdin)" not in provider_adapter
    assert "pagination chain is invalid" in preflight
    assert "provider_snapshot_bytes" in preflight
    assert '{"username": user["username"], "groups": user["groups"]}' in preflight
    assert "provider_subject_snapshot_sha256" in workloads
    assert "provider_trust_anchor_sha256" in workloads
    assert "provider_adapter_sha256" in workloads
    assert "provider_execution_sha256" in workloads
    assert "kubernetes_authentication_sha256" in workloads
    assert "kubernetes_subject_inventory_sha256" in workloads
    assert "effective_rbac_subjects_sha256" in workloads
    assert "provider_collection_authority_sha256" in workloads
    assert "oidc_mapping_sha256" in workloads
    assert "kubernetes_subject_inventory_post_sar_sha256" in workloads
    assert "Kubernetes subject inventory drifted after SubjectAccessReview closure" in preflight
    assert "deadline = time.monotonic() + HANDOFF_SECONDS" in transition
    assert transition.count("connection.settimeout(self._remaining(deadline))") >= 3
    assert "timeout=remaining" in enforcer
    assert "deadline=deadline" in enforcer
    assert "connection.settimeout(remaining)" in enforcer
    assert "auditor_bootstrap_sha256" in workloads
    assert 'import {\n  to = kubernetes_cluster_role_v1.control_plane_network_policy_security_auditor' in boundary
    assert (
        'import {\n  to = kubernetes_cluster_role_binding_v1.control_plane_network_policy_security_auditor'
        in boundary
    )
    assert 'mechanism              = "external-preprovision-declarative-import"' in boundary
    assert "auditor_bootstrap_contract" in preflight
    assert "rotation_binding_contract" in preflight
    assert 'state = "before" if normalized_live == before else "target"' in preflight
    assert 'else "resume"' in preflight
    assert "rotation_binding_state_sha256" in preflight
    assert 'is_binding = resource.startswith("clusterrolebindings.")' in preflight
    assert 'rotation_binding_states[bootstrap_binding_key] == "before"' in preflight
    assert 'get_json(current_bootstrap, query["context"]' in retirement
    assert "binding_evidence" in retirement
    assert "auditor_role_evidence" in retirement


def test_provider_trust_anchor_schema_pins_adapter_and_signing_custody() -> None:
    schema = json.loads(PROVIDER_TRUST_ANCHOR_SCHEMA.read_text())
    properties = schema["properties"]

    assert properties["provider"] == {"const": "nebius-iam"}
    assert properties["adapter"]["properties"]["id"] == {
        "const": "fs2-serve.nebius.ai/nebius-iam-human-directory/v3"
    }
    assert properties["adapter"]["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert properties["directory_query"]["properties"]["cli_path"] == {
        "const": "/usr/local/bin/nebius"
    }
    assert properties["directory_query"]["properties"]["config_path"] == {
        "const": "/etc/fs2/security/nebius-directory-reader.yaml"
    }
    execution = properties["directory_execution"]["properties"]
    authentication = properties["kubernetes_authentication"]["properties"]
    assert execution["cli_path"] == {"const": "/usr/local/bin/nebius"}
    assert execution["credential_path"] == {
        "const": "/etc/fs2/security/nebius-directory-reader-credential.json"
    }
    assert execution["api_endpoint_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert execution["provider_issuer_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    reader_access = execution["directory_reader_access"]["properties"]
    assert reader_access["approved_role"] == {"const": "auditor"}
    assert reader_access["approved_role_effect"] == {
        "const": "view-metadata-without-data-or-mutation"
    }
    assert authentication["groups_claim"] == {"const": "groups"}
    assert authentication["probe_token_path"] == {
        "const": "/etc/fs2/security/network-policy-provider-oidc-probe.jwt"
    }
    assert properties["directory_query"]["properties"]["consistency_passes"] == {"const": 2}
    assert properties["directory_query"]["properties"]["snapshot_ttl_seconds"] == {
        "type": "integer",
        "minimum": 10800,
        "maximum": 28800,
    }
    assert properties["provider_collection_authority_sha256"] == {"$ref": "#/$defs/sha256"}
    authority = json.loads(PROVIDER_COLLECTION_AUTHORITY_SCHEMA.read_text())
    assert authority["properties"]["snapshot_public_key"]["pattern"] == "^[A-Za-z0-9_-]{43}$"
    assert authority["properties"]["provider"] == {"const": "nebius-iam"}
    snapshot = json.loads(PROVIDER_SNAPSHOT_SCHEMA.read_text())
    authorization = snapshot["$defs"]["providerAuthorization"]
    assert authorization["properties"]["cycle"]["properties"]["mode"] == {
        "const": "authorization-directory-directory-authorization"
    }
    authorization_evidence = snapshot["$defs"]["providerAuthorizationEvidence"]
    assert "membership_pages" in authorization_evidence["required"]
    assert "subject_permit_pages" in authorization_evidence["required"]
    assert "effective_permits" in authorization_evidence["required"]
    assert "effective_roles" in authorization_evidence["required"]
    assert "access_contract_sha256" in authorization_evidence["required"]


def test_provider_adapter_enumerates_provider_itself_without_caller_transcript(monkeypatch: Any) -> None:
    query = {
        "cli_path": "/usr/local/bin/nebius",
        "config_path": "/etc/fs2/security/nebius-directory-reader.yaml",
        "profile": "directory-reader",
        "tenant_id": "tenant-example0001",
        "page_size": 200,
        "max_pages": 20,
        "max_records": 100,
        "timeout_seconds": 10,
        "snapshot_ttl_seconds": 10800,
        "consistency_passes": 2,
    }
    authentication = {
        "username_claim": "email",
        "username_prefix": "",
        "groups_prefix": "",
    }
    trust = {
        "directory_execution": {
            "principal_type": "service-account",
            "principal_id": "serviceaccount-directory-reader-001",
            "credential_sha256": "e" * 64,
            "api_endpoint_sha256": "f" * 64,
            "provider_issuer_sha256": "0" * 64,
        },
        "directory_query": query,
        "kubernetes_authentication": authentication,
        "execution_sha256": "1" * 64,
        "kubernetes_authentication_sha256": "2" * 64,
        "tenant_sha256": "a" * 64,
        "query_sha256": "b" * 64,
    }
    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_trust_anchor", lambda: (trust, "d" * 64))
    monkeypatch.setattr(
        PROVIDER_ADAPTER_MODULE,
        "_provider_authority",
        lambda _trust: ({"snapshot_signer_key_id": "c" * 64}, "4" * 64),
    )
    capture_events: list[str] = []

    def authorization_capture(_trust: Any) -> dict[str, str]:
        capture_events.append("authorization")
        return {"authorization": "stable"}

    monkeypatch.setattr(
        PROVIDER_ADAPTER_MODULE,
        "_capture_provider_authorization_once",
        authorization_capture,
    )

    def pages(
        _execution: Any,
        _query: Any,
        command: list[str],
        operation: str,
        _budgets: Any,
    ) -> Any:
        receipt = [{
            "operation": operation,
            "request_token": "",
            "response": {"items": []},
            "next_token": "",
        }]
        if "tenant-user-account-with-attributes" in command:
            capture_events.append("directory")
            _budgets["records"] -= 1
            return ([{
                "tenant_user_account": {
                    "metadata": {"id": "tenantuseraccount-001", "name": "reviewer"}
                },
                "attributes": {"email": "reviewer@example.invalid"},
            }], receipt)
        if command[1:3] == ["group", "list"]:
            _budgets["records"] -= 1
            return ([{
                "metadata": {"id": "group-reviewers-001", "name": "reviewers"}
            }], receipt)
        _budgets["records"] -= 1
        return ([{
            "metadata": {"parent_id": "group-reviewers-001"},
            "spec": {"member_id": "tenantuseraccount-001"},
        }], receipt)

    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_list_pages", pages)
    captured = PROVIDER_ADAPTER_MODULE.capture()

    assert captured["human_users"] == [
        {
            "provider_subject_id": "tenantuseraccount-001",
            "username": "reviewer@example.invalid",
            "groups": ["reviewers"],
        }
    ]
    assert captured["human_groups"] == ["reviewers"]
    assert captured["complete"] is True
    assert captured["adapter"]["id"].endswith("/v3")
    assert captured["pagination"]["consistency"]["passes"] == 2
    assert len(captured["pagination"]["collections"]) == 2
    assert captured["raw_collections"][0] == captured["raw_collections"][1]
    assert capture_events == ["authorization", "directory", "directory", "authorization"]
    assert captured["provider_authorization"]["cycle"]["directory_collection_sha256"] == (
        captured["pagination"]["consistency"]["collection_sha256"]
    )
    captured_at = TRANSITION.dt.datetime.fromisoformat(captured["captured_at"])
    expires_at = TRANSITION.dt.datetime.fromisoformat(captured["expires_at"])
    assert int((expires_at - captured_at).total_seconds()) == query["snapshot_ttl_seconds"]


def test_provider_adapter_collects_subject_parented_access_permits_and_cycle(
    monkeypatch: Any,
) -> None:
    principal_id = "serviceaccount-directory-reader-001"
    group_id = "group-directory-readers-001"
    expected_permits = sorted(
        [
            {
                "parent_id": principal_id,
                "parent_kind": "service-account",
                "resource_id": "tenant-example0001",
                "role": "auditor",
            },
            {
                "parent_id": group_id,
                "parent_kind": "group",
                "resource_id": "tenant-example0001",
                "role": "auditor",
            },
        ],
        key=PROVIDER_ADAPTER_MODULE.canonical,
    )
    trust = {
        "directory_execution": {
            "principal_type": "service-account",
            "principal_id": principal_id,
            "directory_reader_access": {
                "approved_role": "auditor",
                "approved_role_effect": "view-metadata-without-data-or-mutation",
                "expected_permits": expected_permits,
                "expected_permits_sha256": PROVIDER_ADAPTER_MODULE.hashlib.sha256(
                    PROVIDER_ADAPTER_MODULE.canonical(expected_permits).encode()
                ).hexdigest(),
            },
        },
        "directory_query": {
            "tenant_id": "tenant-example0001",
            "page_size": 200,
            "max_pages": 20,
            "max_records": 100,
            "consistency_passes": 2,
        },
    }
    document_calls: list[tuple[str, ...]] = []
    page_calls: list[tuple[str, ...]] = []

    def document(
        _execution: Any,
        _query: Any,
        command: list[str],
        *,
        label: str,
    ) -> dict[str, Any]:
        del label
        document_calls.append(tuple(command))
        if command == ["iam", "whoami"]:
            return {
                "subject": {"type": "service-account", "id": principal_id},
                "tenant_id": "tenant-example0001",
            }
        raise AssertionError(f"unexpected provider document command: {command}")

    def pages(
        _execution: Any,
        _query: Any,
        command: list[str],
        operation: str,
        budgets: dict[str, int],
    ) -> Any:
        page_calls.append(tuple(command))
        budgets["pages"] -= 1
        if "group-membership" in command:
            items = [{
                "metadata": {"parent_id": group_id},
                "spec": {"member_id": principal_id},
            }]
        else:
            subject_id = command[-1]
            assert command[:4] == ["iam", "access-permit", "list", "--parent-id"]
            suffix = "principal" if subject_id == principal_id else "group"
            items = [{
                "metadata": {
                    "id": f"accesspermit-directory-reader-{suffix}-001",
                    "parent_id": subject_id,
                },
                "spec": {"resource_id": "tenant-example0001", "role": "auditor"},
            }]
        budgets["records"] -= len(items)
        return items, [{
            "operation": operation,
            "request_token": "",
            "response": {"items": items, "next_page_token": ""},
            "next_token": "",
        }]

    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_provider_document", document)
    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_list_pages", pages)
    evidence = PROVIDER_ADAPTER_MODULE._capture_provider_authorization_once(trust)
    authorization = PROVIDER_ADAPTER_MODULE._provider_authorization_cycle(
        evidence,
        evidence,
        "d" * 64,
    )

    assert authorization["cycle"]["mode"] == "authorization-directory-directory-authorization"
    assert authorization["cycle"]["directory_collection_sha256"] == "d" * 64
    assert authorization["collections"][0]["evidence"] == authorization["collections"][1][
        "evidence"
    ]
    assert evidence["principal_group_ids"] == [group_id]
    assert {permit["parent_kind"] for permit in evidence["effective_permits"]} == {
        "service-account",
        "group",
    }
    assert evidence["effective_roles"] == ["auditor"]
    assert page_calls.count(
        ("iam", "group-membership", "list-member-of", "--subject-id", principal_id)
    ) == 1
    assert page_calls.count(("iam", "access-permit", "list", "--parent-id", principal_id)) == 1
    assert page_calls.count(("iam", "access-permit", "list", "--parent-id", group_id)) == 1
    assert document_calls == [("iam", "whoami")]
    adapter_source = PROVIDER_ADAPTER.read_text()
    before = adapter_source.index("authorization_before = _capture_provider_authorization_once")
    directory = adapter_source.index("collections = [_capture_directory", before)
    after = adapter_source.index("authorization_after = _capture_provider_authorization_once", directory)
    assert before < directory < after
    with pytest.raises(PROVIDER_ADAPTER_MODULE.AdapterError, match="directory collection fence"):
        PROVIDER_ADAPTER_MODULE._provider_authorization_cycle(
            evidence,
            {**evidence, "effective_roles": ["auditor", "editor"]},
            "d" * 64,
        )


def test_provider_authorization_rejects_a_resource_scoped_mutating_group_permit() -> None:
    principal_id = "serviceaccount-directory-reader-001"
    group_id = "group-directory-readers-001"
    expected_permits = [{
        "parent_id": principal_id,
        "parent_kind": "service-account",
        "resource_id": "tenant-example0001",
        "role": "auditor",
    }]
    trust = {
        "directory_execution": {
            "principal_type": "service-account",
            "principal_id": principal_id,
            "directory_reader_access": {
                "approved_role": "auditor",
                "approved_role_effect": "view-metadata-without-data-or-mutation",
                "expected_permits": expected_permits,
                "expected_permits_sha256": PREFLIGHT.hashlib.sha256(
                    PREFLIGHT.canonical(expected_permits).encode()
                ).hexdigest(),
            },
        },
        "directory_query": {
            "tenant_id": "tenant-example0001",
            "page_size": 200,
            "max_pages": 20,
            "max_records": 100,
            "consistency_passes": 2,
        },
    }
    membership_page = {
        "operation": (
            "group-membership.list-member-of:"
            f"{PREFLIGHT.hashlib.sha256(principal_id.encode()).hexdigest()}"
        ),
        "request_token": "",
        "response": {
            "items": [{
                "metadata": {"parent_id": group_id},
                "spec": {"member_id": principal_id},
            }],
            "next_page_token": "",
        },
        "next_token": "",
    }
    def permit_page(subject_id: str, permit_id: str, resource_id: str, role: str) -> dict[str, Any]:
        return {
            "operation": f"access-permit.list:{PREFLIGHT.hashlib.sha256(subject_id.encode()).hexdigest()}",
            "request_token": "",
            "response": {
                "items": [{
                    "metadata": {"id": permit_id, "parent_id": subject_id},
                    "spec": {"resource_id": resource_id, "role": role},
                }],
                "next_page_token": "",
            },
            "next_token": "",
        }

    principal_page = permit_page(
        principal_id,
        "accesspermit-directory-reader-principal-001",
        "tenant-example0001",
        "auditor",
    )
    group_page = permit_page(
        group_id,
        "accesspermit-directory-reader-group-001",
        "project-resource-scope-001",
        "editor",
    )
    effective_permits = sorted(
        [
            {
                "permit_id": "accesspermit-directory-reader-principal-001",
                **expected_permits[0],
            },
            {
                "permit_id": "accesspermit-directory-reader-group-001",
                "parent_id": group_id,
                "parent_kind": "group",
                "resource_id": "project-resource-scope-001",
                "role": "editor",
            },
        ],
        key=PREFLIGHT.canonical,
    )
    evidence = {
        "whoami": {
            "subject": {"type": "service-account", "id": principal_id},
            "tenant_id": "tenant-example0001",
        },
        "membership_pages": [membership_page],
        "principal_group_ids": [group_id],
        "subject_permit_pages": [
            {
                "subject_id": principal_id,
                "subject_kind": "service-account",
                "pages": [principal_page],
            },
            {
                "subject_id": group_id,
                "subject_kind": "group",
                "pages": [group_page],
            },
        ],
        "effective_permits": effective_permits,
        "effective_roles": ["auditor", "editor"],
        "access_contract_sha256": trust["directory_execution"]["directory_reader_access"][
            "expected_permits_sha256"
        ],
        "page_count": 3,
        "record_count": 3,
    }
    evidence_sha256 = PREFLIGHT.hashlib.sha256(PREFLIGHT.canonical(evidence).encode()).hexdigest()
    directory_sha256 = "d" * 64
    cycle_material = {
        "mode": "authorization-directory-directory-authorization",
        "authorization_before_sha256": evidence_sha256,
        "directory_collection_sha256": directory_sha256,
        "authorization_after_sha256": evidence_sha256,
    }
    authorization = {
        "cycle": {
            **cycle_material,
            "cycle_sha256": PREFLIGHT.hashlib.sha256(
                PREFLIGHT.canonical(cycle_material).encode()
            ).hexdigest(),
        },
        "collections": [
            {"index": index, "phase": phase, "evidence": evidence, "sha256": evidence_sha256}
            for index, phase in enumerate(("before-directory", "after-directory"))
        ],
    }

    with pytest.raises(PREFLIGHT.PreflightError, match="effective access permits"):
        PREFLIGHT.verified_provider_authorization(
            authorization,
            trust,
            directory_collection_sha256=directory_sha256,
        )


def test_provider_oidc_mapping_is_authenticated_by_the_selected_api_server(monkeypatch: Any) -> None:
    claims = {
        "iss": "https://auth.example.invalid",
        "aud": ["kubernetes"],
        "sub": "tenantuseraccount-reviewer-001",
        "email": "reviewer@example.invalid",
        "groups": ["reviewers"],
        "exp": int(TRANSITION.dt.datetime.now(TRANSITION.dt.UTC).timestamp()) + 600,
    }
    encoded_claims = TRANSITION.base64.urlsafe_b64encode(
        PREFLIGHT.canonical(claims).encode()
    ).decode().rstrip("=")
    token_bytes = f"e30.{encoded_claims}.signature\n".encode()
    token_sha256 = PREFLIGHT.hashlib.sha256(token_bytes).hexdigest()
    monkeypatch.setattr(
        PREFLIGHT,
        "descriptor_bytes",
        lambda *_args, **_kwargs: (
            token_bytes,
            SimpleNamespace(
                st_mode=PREFLIGHT.stat.S_IFREG | 0o400,
                st_uid=0,
                st_gid=0,
            ),
        ),
    )
    observed: dict[str, Any] = {}

    def token_review(_kubeconfig: Any, _context: str, *arguments: str, input_text: str | None = None) -> str:
        observed["arguments"] = arguments
        observed["request"] = json.loads(input_text or "{}")
        return PREFLIGHT.canonical({
            "status": {
                "authenticated": True,
                "audiences": ["kubernetes"],
                "user": {
                    "username": "reviewer@example.invalid",
                    "uid": "oidc-user-uid-001",
                    "groups": ["reviewers", "system:authenticated"],
                    "extra": {},
                },
            }
        })

    monkeypatch.setattr(PREFLIGHT, "run", token_review)
    trust = {
        "kubernetes_authentication": {
            "probe_token_sha256": token_sha256,
            "oidc_issuer": claims["iss"],
            "oidc_issuer_sha256": PREFLIGHT.hashlib.sha256(claims["iss"].encode()).hexdigest(),
            "audiences": ["kubernetes"],
            "audiences_sha256": PREFLIGHT.hashlib.sha256(
                PREFLIGHT.canonical(["kubernetes"]).encode()
            ).hexdigest(),
            "username_claim": "email",
            "username_prefix": "",
            "groups_claim": "groups",
            "groups_prefix": "",
        }
    }
    evidence_sha256 = PREFLIGHT.verified_cluster_oidc_mapping(
        PREFLIGHT.Path("/run/bootstrap-kubeconfig"),
        "reviewed-context",
        trust=trust,
        provider_users=[{
            "provider_subject_id": claims["sub"],
            "username": claims["email"],
            "groups": claims["groups"],
        }],
        cluster=("https://api.example.invalid", "kube-system-uid-000000000000"),
    )

    assert observed["arguments"] == (
        "create",
        "--raw",
        "/apis/authentication.k8s.io/v1/tokenreviews",
        "-f",
        "-",
    )
    assert observed["request"]["spec"]["token"] == token_bytes.decode().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", evidence_sha256)


def test_kubernetes_rbac_binding_subjects_are_in_the_authorization_closure() -> None:
    role_bindings = {
        "tenant-a": [
            {
                "subjects": [
                    {"kind": "User", "name": "user@example.invalid"},
                    {"kind": "Group", "name": "platform-reviewers"},
                    {"kind": "ServiceAccount", "name": "worker", "namespace": "tenant-a"},
                ]
            }
        ]
    }
    cluster_role_bindings = [
        {
            "subjects": [
                {"kind": "Group", "name": "system:authenticated"},
                {"kind": "User", "name": "user@example.invalid"},
            ]
        }
    ]

    subjects = PREFLIGHT.rbac_binding_authorization_subjects(
        role_bindings,
        cluster_role_bindings,
    )

    assert {subject["username"] for subject in subjects} >= {
        "user@example.invalid",
        "system:serviceaccount:tenant-a:worker",
    }
    assert any(subject["groups"] == ["platform-reviewers"] for subject in subjects)
    assert any(subject["groups"] == ["system:authenticated"] for subject in subjects)
    service_account = next(
        subject
        for subject in subjects
        if subject["username"] == "system:serviceaccount:tenant-a:worker"
    )
    assert service_account["groups"] == [
        "system:authenticated",
        "system:serviceaccounts",
        "system:serviceaccounts:tenant-a",
    ]
    assert sum(subject["username"] == "user@example.invalid" for subject in subjects) == 1


def test_provider_adapter_rejects_a_hybrid_directory_across_consistency_passes(
    monkeypatch: Any,
) -> None:
    trust = {
        "directory_query": {"consistency_passes": 2, "snapshot_ttl_seconds": 10800},
    }
    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_trust_anchor", lambda: (trust, "d" * 64))
    monkeypatch.setattr(
        PROVIDER_ADAPTER_MODULE,
        "_provider_authority",
        lambda _trust: ({"snapshot_signer_key_id": "c" * 64}, "4" * 64),
    )
    monkeypatch.setattr(
        PROVIDER_ADAPTER_MODULE,
        "_capture_provider_authorization_once",
        lambda _trust: {},
    )
    collections = iter(
        [
            {
                "users": [{"username": "first@example.invalid", "groups": ["reviewers"]}],
                "groups": ["reviewers"],
                "raw_pages": [{"response": {"items": ["first"]}}],
                "receipts": [],
                "record_count": 2,
                "collection_sha256": "1" * 64,
            },
            {
                "users": [{"username": "second@example.invalid", "groups": ["reviewers"]}],
                "groups": ["reviewers"],
                "raw_pages": [{"response": {"items": ["second"]}}],
                "receipts": [],
                "record_count": 2,
                "collection_sha256": "2" * 64,
            },
        ]
    )
    monkeypatch.setattr(PROVIDER_ADAPTER_MODULE, "_capture_directory", lambda _trust: next(collections))

    with pytest.raises(PROVIDER_ADAPTER_MODULE.AdapterError, match="repeat-stability"):
        PROVIDER_ADAPTER_MODULE.capture()


def test_external_rbac_boundary_separates_runtime_audit_and_subject_rotation() -> None:
    boundary = BOUNDARY_TERRAFORM.read_text()
    preflight = BOUNDARY_PREFLIGHT.read_text()
    retirement = EPOCH_RETIREMENT.read_text()

    imported_roles = (
        "kubernetes_cluster_role_v1.control_plane_network_policy_security_owner",
        "kubernetes_cluster_role_v1.control_plane_network_policy_security_auditor",
        "kubernetes_cluster_role_v1.control_plane_network_policy_security_bootstrap",
        "kubernetes_role_v1.control_plane_network_policy_transition_state",
        "kubernetes_role_v1.control_plane_network_policy_transition_state_bootstrap",
        "kubernetes_role_v1.control_plane_network_policy_transition_gateway",
        "kubernetes_role_v1.control_plane_network_policy_transition_gateway_bootstrap",
        "kubernetes_role_v1.control_plane_network_policy_transition_controller",
        "kubernetes_role_v1.control_plane_network_policy_transition_controller_bootstrap",
    )
    for resource in imported_roles:
        assert f"to = {resource}" in boundary
    assert boundary.count("ignore_changes  = all") >= 9
    assert 'message    = "externally owned permanent Role and ClusterRole definitions are immutable"' in boundary
    assert (
        'message    = "permanent RBAC bindings require the exact next-epoch role '
        'and user-only subject set"'
    ) in boundary
    assert "subject.kind == 'User'" in boundary
    assert "size(object.subjects) == 3" in boundary
    assert "size(object.subjects) == 2" in boundary

    auditor = boundary.split(
        'resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_auditor"',
        maxsplit=1,
    )[1].split('resource "kubernetes_cluster_role_binding_v1"', maxsplit=1)[0]
    assert 'resources  = ["rolebindings"]' not in auditor
    assert 'resources  = ["certificatesigningrequests"]' in auditor
    assert 'verbs          = ["get"]' in auditor

    runtime_state = boundary.split(
        'resource "kubernetes_role_v1" "control_plane_network_policy_transition_state"',
        maxsplit=1,
    )[1].split('resource "kubernetes_cluster_role_v1"', maxsplit=1)[0]
    assert 'resources      = ["rolebindings"]' not in runtime_state
    assert 'resources  = ["clusterroles", "clusterrolebindings"]' in boundary
    assert 'verbs = ["get", "patch", "update"]' in boundary
    assert "for identity in (release, security, prior_security):" in preflight
    assert 'can_i(identity, context, "no", verb, resource' in preflight
    assert "external_role_contract" in preflight
    assert "external_role_bundle_sha256" in preflight
    assert "external_role_bundle_evidence" in retirement
    assert "current_bootstrap_kubeconfig" in retirement


def test_rbac_inventory_derives_named_impersonation_delegation_and_custom_signers() -> None:
    roles = {
        "tenant-a": [
            {
                "name": "named-grants",
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["users", "groups", "serviceaccounts"],
                        "resourceNames": ["future-principal"],
                        "verbs": ["impersonate"],
                    },
                    {
                        "apiGroups": ["rbac.authorization.k8s.io"],
                        "resources": ["roles"],
                        "resourceNames": ["future-role"],
                        "verbs": ["bind"],
                    },
                ],
            }
        ]
    }
    cluster_roles = [
        {
            "name": "signer-grant",
            "rules": [
                {
                    "apiGroups": ["certificates.k8s.io"],
                    "resources": ["signers"],
                    "resourceNames": ["example.invalid/custom-signer"],
                    "verbs": ["approve"],
                }
            ],
        }
    ]

    impersonation, delegation, signers = PREFLIGHT.rbac_rule_authorization_targets(
        roles, cluster_roles
    )

    assert ("users", "future-principal", "") in impersonation
    assert ("groups", "future-principal", "") in impersonation
    assert ("serviceaccounts", "future-principal", "tenant-a") in impersonation
    assert ("roles.rbac.authorization.k8s.io", "future-role", "tenant-a") in delegation
    assert signers == {"example.invalid/custom-signer"}


def test_rotation_binding_contract_accepts_only_exact_crash_resume_states(monkeypatch: Any) -> None:
    before = ["prior-owner", "current-owner", "current-bootstrap"]
    target = ["current-owner", "successor-owner", "successor-bootstrap"]

    def binding(subjects: list[str]) -> dict[str, Any]:
        return {
            "metadata": {
                "name": "fs2-network-policy-security-owner",
                "uid": "binding-uid-000000000000",
                "resourceVersion": "42",
                "labels": {"fs2.nebius.ai/network-policy-boundary": "permanent"},
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "fs2-network-policy-security-owner",
            },
            "subjects": [
                {"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": subject}
                for subject in subjects
            ],
        }

    live = binding(before)
    monkeypatch.setattr(PREFLIGHT, "run", lambda *_args, **_kwargs: json.dumps(live))
    evidence, state = PREFLIGHT.rotation_binding_contract(
        PREFLIGHT.Path("/run/fs2/bootstrap.kubeconfig"),
        "reviewed",
        resource="clusterrolebinding",
        name="fs2-network-policy-security-owner",
        namespace="",
        role_kind="ClusterRole",
        role_name="fs2-network-policy-security-owner",
        before_subjects=before,
        target_subjects=target,
    )
    assert state == "before"
    assert evidence["state"] == "before"

    live = binding(target)
    _, state = PREFLIGHT.rotation_binding_contract(
        PREFLIGHT.Path("/run/fs2/bootstrap.kubeconfig"),
        "reviewed",
        resource="clusterrolebinding",
        name="fs2-network-policy-security-owner",
        namespace="",
        role_kind="ClusterRole",
        role_name="fs2-network-policy-security-owner",
        before_subjects=before,
        target_subjects=target,
    )
    assert state == "target"

    live = binding(["unexpected-owner"])
    with pytest.raises(PREFLIGHT.PreflightError, match="outside its exact before/target states"):
        PREFLIGHT.rotation_binding_contract(
            PREFLIGHT.Path("/run/fs2/bootstrap.kubeconfig"),
            "reviewed",
            resource="clusterrolebinding",
            name="fs2-network-policy-security-owner",
            namespace="",
            role_kind="ClusterRole",
            role_name="fs2-network-policy-security-owner",
            before_subjects=before,
            target_subjects=target,
        )


def test_manual_helm_uninstall_or_replace_cannot_delete_boundary_objects() -> None:
    template = (CHART / "templates" / "networkpolicy.yaml").read_text()
    boundary = BOUNDARY_TERRAFORM.read_text()

    for name in (
        "public-envoy-transition-guard",
        "envoy-controller-xds-transition-guard",
        "envoy-default-deny",
    ):
        assert name not in template
        assert name in boundary
    assert 'operations  = ["UPDATE", "DELETE"]' in boundary
    assert "permanent boundary objects are never deleted" in boundary
    assert "expression = \"request.operation != 'DELETE'\"" in boundary
    assert "decommission-receipt-sha256" not in boundary
    assert 'validationActions = ["Deny"]' in boundary
    assert "ignore_changes  = [manifest.spec.validationActions]" in boundary
