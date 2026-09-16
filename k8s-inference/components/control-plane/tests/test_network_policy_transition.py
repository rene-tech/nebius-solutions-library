# ruff: noqa: S603 -- fixed repository helper and test-owned command fakes.
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import sys
from types import ModuleType
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
HANDOFF_SCHEMA = CONTROL_ROOT / "contracts" / "network-policy-security-handoff.schema.json"


def _load_transition_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fs2_network_policy_transition", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TRANSITION = _load_transition_module()

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
                "security_owner_username": "fs2-network-policy-security-owner",
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
    transition.arguments = type("Arguments", (), {"revision": "7", "timeout": "10m"})()
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

    assert 'exec python3 "${script_dir}/network_policy_transition.py" "$@"' in wrapper
    assert "--all-namespaces" not in source
    assert '"--namespace", namespace' in source
    assert '"create",\n            "token"' not in source
    assert "SERVICE_ACCOUNT" not in source
    assert 'parser.add_argument("--security-handoff-socket", required=True)' in source
    assert 'parser.add_argument("--security-handoff-public-key", required=True)' in source
    assert "security_owner_kubeconfig" not in source
    assert 'action not in {"attest", "patch-exact-kubernetes-object", "set-admission-recovery"}' in source
    schema = json.loads(HANDOFF_SCHEMA.read_text())
    request_action = schema["$defs"]["request"]["properties"]["action"]["enum"]
    assert request_action == ["attest", "patch-exact-kubernetes-object", "set-admission-recovery"]
    assert "delete" not in request_action


def test_signed_handoff_schema_rejects_generic_or_delete_shaped_requests() -> None:
    schema = json.loads(HANDOFF_SCHEMA.read_text())
    validator = Draft202012Validator(schema)
    base = {
        "schema": "fs2-serve.nebius.ai/network-policy-security-handoff-request/v1",
        "operation_id": "b33f7e4c-bf2f-48ec-a0d3-df64f2393880",
        "cluster": {"api_server_sha256": "a" * 64, "kube_system_uid": "8af2b70d-25f7-4f0e-9ad0-776abc8a61e2"},
        "release": {"name": "fs2-serve-control-plane", "namespace": "fs2-system"},
        "issued_at": "2026-09-16T16:00:00Z",
        "expires_at": "2026-09-16T16:00:30Z",
    }
    patch = {
        **base,
        "action": "patch-exact-kubernetes-object",
        "body": {
            "resource": "networkpolicy",
            "name": "fs2-serve-control-plane-public-envoy-transition-guard",
            "namespace": "envoy-gateway-system",
            "patch_type": "merge",
            "patch": {"metadata": {"resourceVersion": "17"}},
            "dry_run": True,
        },
    }
    recovery = {
        **base,
        "action": "set-admission-recovery",
        "body": {
            "mode": "audit-warn",
            "recovery_reference": "SEC-1234",
            "topology_uid": "8af2b70d-25f7-4f0e-9ad0-776abc8a61e2",
            "topology_sha256": "b" * 64,
            "receipt": {
                "namespace": "fs2-system",
                "name": "fs2-network-policy-transition",
                "uid": "52231eb2-e71d-4791-875f-1f72baeb573f",
                "resource_version": "19",
                "sha256": "c" * 64,
            },
            "lease": {
                "namespace": "fs2-system",
                "name": "fs2-network-policy-transition",
                "uid": "e31e7ee2-a6f7-4c76-a48a-c10662a0622e",
                "resource_version": "21",
                "holder_identity": "test-holder",
                "lease_transitions": 3,
            },
            "binding": "fs2-network-policy-boundary",
            "parameter": {"namespace": "fs2-system", "name": "fs2-network-policy-boundary-parameters"},
            "delete_allowed": False,
        },
    }

    assert validator.is_valid(patch)
    assert validator.is_valid(recovery)
    assert not validator.is_valid({**patch, "action": "delete"})
    assert not validator.is_valid({**patch, "body": {**patch["body"], "name": "foreign-policy"}})
    assert not validator.is_valid({**recovery, "body": {**recovery["body"], "delete_allowed": True}})


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
        "patch" in call and any("/fs2-network-policy-boundary" in item for item in call)
        for call in transition.bootstrap_kubectl.calls
    )
    assert any("proxy-guard" in item for call in transition.bootstrap_kubectl.calls for item in call)
    parameter_name = "fs2-network-policy-boundary-parameters"
    assert any(parameter_name in item for call in transition.bootstrap_kubectl.calls for item in call)
    assert any("deletecollection" in call for call in transition.bootstrap_kubectl.calls)
    assert any("--subresource=token" in call for call in transition.bootstrap_kubectl.calls)
    assert all(call[:2] == ("auth", "can-i") for call in transition.bootstrap_kubectl.calls)

    def allowed(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        return _result(stdout="yes\n")

    transition.bootstrap_kubectl = FakeCommand(allowed)
    with pytest.raises(TRANSITION.TransitionError, match="external security boundary"):
        transition.verify_external_iam_boundary(topology)


def test_signed_handoff_must_bind_the_same_cluster_identity() -> None:
    transition = _bare_transition()
    public_key = TRANSITION.base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    transition.security_handoff_socket = TRANSITION.Path("/run/fs2/security.sock")
    transition.security_handoff_public_key = public_key
    transition.bootstrap_kubectl = FakeCommand(lambda *_args: _result())
    transition._cluster_identity = lambda _command: ("https://api-one", "uid-one")
    transition._protected_topology = lambda _command: {
        "contract": {
            "security_owner_username": "external-security-owner",
            "security_handoff": {
                "schema": "fs2-serve.nebius.ai/network-policy-security-handoff/v1",
                "socket_path": "/run/fs2/security.sock",
                "public_key": public_key,
                "public_key_sha256": TRANSITION.sha256_text(public_key),
                "cluster": {"api_server_sha256": "different", "kube_system_uid": "uid-two"},
                "delete_allowed": False,
                "recovery_modes": ["Audit", "Warn", "Deny"],
            },
        }
    }

    with pytest.raises(TRANSITION.TransitionError, match="same-cluster topology"):
        transition.configure_guarded_client()


def test_signed_handoff_verifies_request_cluster_expiry_and_ed25519_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = TRANSITION.base64.urlsafe_b64encode(private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    cluster = {"api_server_sha256": "a" * 64, "kube_system_uid": "cluster-uid"}
    handoff = TRANSITION.SecurityHandoff(
        TRANSITION.Path("/run/fs2/security.sock"),
        public_key,
        cluster=cluster,
        release={"name": "test-release", "namespace": "fs2-system"},
    )

    def exchange(request: dict[str, Any]) -> dict[str, Any]:
        issued = TRANSITION.dt.datetime.now(TRANSITION.dt.UTC)
        signed = {
            "schema": "fs2-serve.nebius.ai/network-policy-security-handoff-response/v1",
            "operation_id": request["operation_id"],
            "request_sha256": TRANSITION.sha256_json(request),
            "cluster": cluster,
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "expires_at": (issued + TRANSITION.dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            "status": "approved",
            "signer_key_id": handoff.key_id,
            "result": {"attested": True},
        }
        signature = TRANSITION.base64.urlsafe_b64encode(private_key.sign(TRANSITION.canonical(signed).encode()))
        return {"signed": signed, "signature": signature.decode().rstrip("=")}

    handoff._exchange = exchange
    assert handoff.request("attest", {}) == {"attested": True}

    handoff._exchange = lambda request: {**exchange(request), "signature": "A" * 86}
    with pytest.raises(TRANSITION.TransitionError, match="signature is invalid"):
        handoff.request("attest", {})


def test_protected_command_routes_only_exact_patches_to_signed_handoff() -> None:
    ordinary = FakeCommand(lambda _arguments, _kwargs: _result(stdout='{"kind":"ConfigMap"}'))

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
    )
    assert json.loads(command.run("get", "configmap", "name", "--namespace", "fs2-system", "-o", "json").stdout)[
        "kind"
    ] == "ConfigMap"
    patched = command.run(
        "patch",
        "networkpolicy",
        "guard",
        "--namespace",
        "edge-custom",
        "--type=merge",
        "--patch",
        '{"metadata":{"resourceVersion":"17"}}',
        "--dry-run=server",
        "-o",
        "json",
    )
    assert json.loads(patched.stdout)["kind"] == "NetworkPolicy"
    assert handoff.calls == [
        (
            "patch-exact-kubernetes-object",
            {
                "resource": "networkpolicy",
                "name": "guard",
                "namespace": "edge-custom",
                "patch_type": "merge",
                "patch": {"metadata": {"resourceVersion": "17"}},
                "dry_run": True,
            },
        )
    ]
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
            '{"metadata":{"resourceVersion":"17"}}',
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
                    stdout=json.dumps(
                        [{"revision": 140, "status": "deployed", "description": "Rollback to 138"}]
                    )
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
    transition.live_topology = lambda: {"uid": "topology-uid", "sha256": "a" * 64}
    transition.receipt = lambda: (
        {
            "metadata": {
                "namespace": "fs2-system",
                "name": "fs2-network-policy-transition",
                "uid": "receipt-uid",
                "resourceVersion": "17",
            }
        },
        {"phase": "active"},
    )
    transition._lease = lambda: {
        "metadata": {
            "namespace": "fs2-system",
            "name": "fs2-network-policy-transition",
            "uid": "lease-uid",
            "resourceVersion": "18",
        },
        "spec": {"holderIdentity": "test-holder", "leaseTransitions": 3},
    }

    class Handoff:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def request(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((action, body))
            actions = ["Audit", "Warn"] if body["mode"] == "audit-warn" else ["Deny"]
            return {
                "binding": {
                    "metadata": {
                        "name": "fs2-network-policy-boundary",
                        "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
                    },
                    "spec": {
                        "policyName": "fs2-network-policy-boundary",
                        "validationActions": actions,
                    },
                },
                "parameter": {
                    "metadata": {
                        "namespace": "fs2-system",
                        "name": "fs2-network-policy-boundary-parameters",
                        "labels": {TRANSITION.BOUNDARY_LABEL: TRANSITION.BOUNDARY_VALUE},
                    },
                    "data": {"mode": body["mode"], "delete_allowed": "false"},
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


def test_foundation_security_owner_permanently_owns_boundary_outside_workloads() -> None:
    boundary = BOUNDARY_TERRAFORM.read_text()
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
    assert "request.userInfo.username == '${local.control_plane_network_policy_security_owner}'" in boundary
    assert "system:serviceaccount:fs2-system" not in boundary
    assert 'resource "kubernetes_service_account_v1" "control_plane_network_policy_transition"' in boundary
    assert "automount_service_account_token = false" in boundary
    assert 'kind      = "ServiceAccount"' not in boundary
    assert "decommission-receipt-sha256" not in boundary
    assert "permanent boundary objects are never deleted" in boundary
    assert boundary.count("prevent_destroy = true") >= 19
    assert boundary.count("provider = kubernetes.network_policy_security_owner") == 18
    assert 'ordinary_can "$verb" "$resource/fs2-network-policy-boundary"' in boundary
    assert 'ordinary_can deletecollection "$resource"' in boundary
    assert 'security_can deletecollection "$resource"' in boundary
    assert 'security_can delete "$resource/fs2-network-policy-boundary"' in boundary
    assert 'security_can delete "$resource/fs2-network-policy-boundary")" = "no"' in boundary
    assert 'ordinary_can "$verb" "$resource/$name" --namespace "$namespace"' in boundary
    assert "fs2-network-policy-boundary-parameters" in boundary
    assert 'ordinary_can impersonate "users/$FS2_SECURITY_OWNER_USERNAME"' in boundary
    assert "serviceaccounts/fs2-network-policy-transition --subresource=token" in boundary
    assert "get namespace kube-system -o 'jsonpath={.metadata.uid}'" in boundary
    assert 'test "$ordinary_server" = "$security_server"' in boundary
    assert "auth whoami -o json" in boundary
    assert 'security_can "$verb" "$resource/fs2-network-policy-boundary"' in boundary
    assert 'operations  = ["UPDATE", "DELETE"]' in boundary
    assert 'validationActions = ["Deny"]' in boundary
    assert '["Audit", "Warn", "Deny"]' in boundary
    assert re.search(r"delete_allowed\s+= false", boundary)
    assert 'verbs          = ["get", "patch", "update", "delete"]' not in boundary
    assert "resource_names = [" in boundary
    assert 'resources  = ["pods"]' in boundary
    assert 'verbs      = ["get", "list"]' in boundary
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
    assert "--security-handoff-public-key" in control_plane
    assert "stages/workloads/control_plane_network_policy_boundary.tf" not in control_plane


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
    assert 'expression = "request.operation != \'DELETE\'"' in boundary
    assert "decommission-receipt-sha256" not in boundary
    assert 'validationActions = ["Deny"]' in boundary
    assert 'ignore_changes  = [manifest.spec.validationActions]' in boundary
