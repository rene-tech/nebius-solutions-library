# ruff: noqa: S603 -- fixed repository helper and test-owned command fakes.
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from types import ModuleType
from typing import Any

import pytest
from conftest import CONTROL_ROOT

SCRIPT = CONTROL_ROOT / "scripts" / "network_policy_transition.py"
WRAPPER = CONTROL_ROOT / "scripts" / "network-policy-transition.sh"
SOLUTION_ROOT = CONTROL_ROOT.parents[1]
CHART = SOLUTION_ROOT / "charts" / "control-plane" / "fs2-serve-control-plane"
TERRAFORM = SOLUTION_ROOT / "stages" / "workloads" / "control_plane.tf"
BOUNDARY_TERRAFORM = SOLUTION_ROOT / "stages" / "workloads" / "control_plane_network_policy_boundary.tf"


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
        network_policy_render_sha256="d" * 64,
        release={
            "name": "test-release",
            "namespace": "fs2-system",
            "revision": "138",
            "status": "deployed",
            "deployed_manifest_sha256": "e" * 64,
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
    assert '"create",\n            "token"' in source
    assert 'SERVICE_ACCOUNT = "fs2-network-policy-transition"' in source


def test_ready_coverage_requires_a_ready_selected_pod() -> None:
    transition = _bare_transition()

    def no_pods(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert arguments[:4] == ("get", "pods", "--namespace", "edge-custom")
        return _result(stdout='{"items":[]}')

    transition.guarded_kubectl = FakeCommand(no_pods)
    with pytest.raises(TRANSITION.TransitionError, match="selects zero Ready Pods"):
        transition.verify_ready_pods("edge-custom", PROXY_SPEC, role="public-envoy")

    def ready_pod(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        assert "--selector" in arguments
        assert arguments[arguments.index("--selector") + 1] == "app.kubernetes.io/name=envoy"
        return _result(
            stdout=json.dumps({"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]})
        )

    transition.guarded_kubectl = FakeCommand(ready_pod)
    assert transition.verify_ready_pods("edge-custom", PROXY_SPEC, role="public-envoy") == 1


def test_strict_stage_rejects_absent_release_before_any_boundary_mutation() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    candidate.release["status"] = "absent"
    transition.render_candidate = lambda: candidate

    with pytest.raises(TRANSITION.TransitionError, match="requires an existing release"):
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

    assert writes == [
        (
            "bootstrap-ready",
            {"extra": {"bootstrap": {"deny_proof": "relaxed-zero-selected-pods"}}},
        )
    ]


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
            "candidate": {"candidate_sha256": candidate.candidate_sha256},
        },
    )
    transition.get_policy = lambda name, _namespace: {
        candidate.proxy["guard_name"]: proxy,
        candidate.controller["guard_name"]: controller,
        candidate.deny_name: deny,
    }[name]
    transition.verify_receipt_boundaries = lambda receipt, _boundaries: verified_phases.append(receipt["phase"])

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

    assert verified_phases == ["bootstrap-ready", "staged"]
    assert events.index("ready:public-envoy") < events.index("deny:active")
    assert events.index("ready:envoy-controller") < events.index("deny:active")
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
    assert captured["candidate"]["release"]["revision"] == "138"
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
    transition.render_candidate = lambda: candidate
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: (
        {},
        {
            "phase": state["phase"],
            "candidate": {"candidate_sha256": candidate.candidate_sha256},
        },
    )
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
    assert "delete" not in SCRIPT.read_text()


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
    state = {"phase": "staged", "manifest": "current", "deny": "active"}
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
            "candidate": {"candidate_sha256": candidate.candidate_sha256},
            "boundary_objects": {
                "public-envoy": TRANSITION.Transition._guard_receipt(proxy),
                "envoy-controller": TRANSITION.Transition._guard_receipt(controller),
                "default-deny": TRANSITION.Transition._guard_receipt(deny),
            },
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

    def relax(_candidate: Any) -> str:
        state["deny"] = "relaxed"
        return "relaxed-zero-selected-pods"

    transition.relax_deny = relax

    def write(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        writes.append(phase)
        state["phase"] = phase
        return {}

    transition.write_receipt = write

    class FailingHelm:
        @staticmethod
        def run(*_args: str, **_kwargs: Any) -> Any:
            raise TRANSITION.TransitionError("injected rollback failure")

    transition.helm = FailingHelm()
    with pytest.raises(TRANSITION.TransitionError, match="injected rollback failure"):
        transition.rollback()

    assert state["deny"] == "relaxed"
    assert writes == ["rollback-prepared"]


def test_terraform_owns_boundary_and_enforces_scoped_rbac_admission_and_destroy_order() -> None:
    boundary = BOUNDARY_TERRAFORM.read_text()
    control_plane = TERRAFORM.read_text()

    for resource in (
        'resource "kubernetes_network_policy_v1" "control_plane_public_envoy_boundary"',
        'resource "kubernetes_network_policy_v1" "control_plane_envoy_controller_boundary"',
        'resource "kubernetes_network_policy_v1" "control_plane_envoy_default_deny"',
        'resource "kubernetes_manifest" "control_plane_network_policy_transition_lease"',
        'resource "kubernetes_config_map_v1" "control_plane_network_policy_transition_receipt"',
        'resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission"',
    ):
        assert resource in boundary
    assert '"fs2.nebius.ai/network-policy-boundary" = "permanent"' in boundary
    assert "request.userInfo.username == 'system:serviceaccount:fs2-system:" in boundary
    assert 'operations  = ["UPDATE", "DELETE"]' in boundary
    assert "resource_names = [" in boundary
    assert 'resources  = ["pods"]' in boundary
    assert 'verbs      = ["get", "list"]' in boundary
    assert "cluster_role" not in boundary.lower()
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
    assert "permanent NetworkPolicy boundaries may only be changed" in boundary
