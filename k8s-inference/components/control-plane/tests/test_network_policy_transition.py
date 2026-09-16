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
BOUNDARY_TERRAFORM = SOLUTION_ROOT / "stages" / "foundation" / "control_plane_network_policy_boundary.tf"


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
        complete_render_sha256="e" * 64,
        network_policy_render_sha256="d" * 64,
        release={
            "name": "test-release",
            "namespace": "fs2-system",
            "revision": "138",
            "status": "deployed",
            "deployed_manifest_sha256": "e" * 64,
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
    assert '"create",\n            "token"' in source
    assert 'SERVICE_ACCOUNT = "fs2-network-policy-transition"' in source


def test_rollout_identity_must_not_own_or_impersonate_security_boundary() -> None:
    transition = _bare_transition()

    topology = {"data": {"topology.json": json.dumps({"security_owner_username": "external-security-owner"})}}

    def denied(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        if arguments[:2] == ("get", "configmap"):
            return _result(stdout=json.dumps(topology))
        return _result(stdout="no\n")

    transition.bootstrap_kubectl = FakeCommand(denied)
    transition.verify_external_iam_boundary()
    assert len(transition.bootstrap_kubectl.calls) == 6
    assert transition.bootstrap_kubectl.calls[-1][-1] == "users/external-security-owner"
    assert all(call[:2] == ("auth", "can-i") for call in transition.bootstrap_kubectl.calls[1:])

    def allowed(arguments: tuple[str, ...], _kwargs: dict[str, Any]) -> Any:
        if arguments[:2] == ("get", "configmap"):
            return _result(stdout=json.dumps(topology))
        return _result(stdout="yes\n")

    transition.bootstrap_kubectl = FakeCommand(allowed)
    with pytest.raises(TRANSITION.TransitionError, match="external security boundary"):
        transition.verify_external_iam_boundary()


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

    assert writes == [
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
    transition.verify_local_candidate = lambda _receipt: candidate
    deployed_release = {**candidate.release, "revision": "139"}
    transition.verify_deployed_target = lambda _candidate: deployed_release

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


def test_rollback_rejects_arbitrary_revision_and_debug_enabled_target() -> None:
    transition = _bare_transition()
    candidate = _candidate()
    with pytest.raises(TRANSITION.TransitionError, match="receipt-bound stable source"):
        transition._rollback_target(candidate, "7")

    transition._yaml_documents = lambda _text: [{"kind": "ConfigMap"}]
    candidate.release["revision"] = "138"
    candidate.release["deployed_manifest_sha256"] = TRANSITION.sha256_json([{"kind": "ConfigMap"}])

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
        transition._rollback_target(candidate, "138")


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
    transition.render_candidate = lambda: candidate
    transition.lock = contextlib.nullcontext
    transition.receipt = lambda: (
        {},
        {
            "phase": state["phase"],
            "candidate": candidate.as_dict(),
            "deployed_release": deployed_release,
        },
    )
    transition.verify_local_candidate = lambda _receipt: candidate
    transition.verify_deployed_target = lambda _candidate: deployed_release
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
            "candidate": candidate.as_dict(),
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
    transition.verify_live_topology = lambda _candidate: None
    target_hash = TRANSITION.sha256_json([{"manifest": "target"}])
    transition._rollback_target = lambda _candidate, _revision: {
        "target_revision": "7",
        "target_manifest_sha256": target_hash,
        "target_values_sha256": "f" * 64,
        "request_debug_enabled": False,
    }
    transition._release_identity = lambda *_args, **_kwargs: {**candidate.release, "revision": "139"}
    transition._yaml_documents = lambda text: [{"manifest": text}]

    def relax(_candidate: Any) -> str:
        state["deny"] = "relaxed"
        return "relaxed-zero-selected-pods"

    transition.relax_deny = relax

    def write(phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        writes.append(phase)
        state["phase"] = phase
        return {}

    transition.write_receipt = write

    transition.run_fenced_helm = lambda *_args: (_ for _ in ()).throw(
        TRANSITION.TransitionError("injected rollback failure")
    )
    with pytest.raises(TRANSITION.TransitionError, match="injected rollback failure"):
        transition.rollback()

    assert state["deny"] == "relaxed"
    assert writes == ["rollback-prepared"]


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
        'resource "kubernetes_manifest" "control_plane_network_policy_boundary_admission"',
        'resource "kubernetes_cluster_role_v1" "control_plane_network_policy_security_owner"',
    ):
        assert resource in boundary
        assert resource not in control_plane
    assert '"fs2.nebius.ai/network-policy-boundary" = "permanent"' in boundary
    assert "request.userInfo.username == '${local.control_plane_network_policy_security_owner}'" in boundary
    assert "system:serviceaccount:fs2-system:${local.control_plane_network_policy_service_account}" in boundary
    assert "decommission-receipt-sha256" in boundary
    assert boundary.count("prevent_destroy = true") >= 18
    assert boundary.count("provider = kubernetes.network_policy_security_owner") == 17
    assert "ordinary_can delete validatingadmissionpolicybindings" in boundary
    assert 'ordinary_can impersonate "users/$FS2_SECURITY_OWNER_USERNAME"' in boundary
    assert "auth whoami -o json" in boundary
    assert "security_can delete validatingadmissionpolicybindings" in boundary
    assert 'operations  = ["UPDATE", "DELETE"]' in boundary
    assert "resource_names = [" in boundary
    assert 'resources  = ["pods"]' in boundary
    assert 'verbs      = ["get", "list"]' in boundary
    assert 'resources      = ["networkpolicies"]' in boundary
    assert 'resources      = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]' in boundary
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
    assert "permanent boundary deletion requires the external security owner" in boundary
    assert "request.operation != 'DELETE'" in boundary
    assert "decommission-receipt-sha256" in boundary
