import copy
import subprocess

import yaml

from prepare_candidate import (
    ANNOTATION,
    IMAGE,
    MODEL,
    ROOT,
    candidate_template,
    isolated,
    proposal,
)


def baseline():
    resources = list(
        yaml.safe_load_all(
            (ROOT / "models/general-media/k8s/nv-reason-cxr-3b.yaml").read_text()
        )
    )
    return {
        "modelRef": MODEL,
        "runtimeContainerName": "vllm",
        "resources": resources,
        "templateDigest": "old",
    }


def test_template_changes_only_identity_instrumentation_and_preserves_model_limits():
    before = baseline()
    unchanged = copy.deepcopy(before)
    after = candidate_template(before)
    assert before == unchanged
    assert after["templateDigest"] != before["templateDigest"]
    assert len(after["resources"]) == len(before["resources"]) + 1
    cm = after["resources"].pop()
    assert cm["immutable"] is True
    assert (
        cm["data"]["fs2_runtime_identity.py"]
        == (ROOT / "models/general-media/fs2_runtime_identity.py").read_text()
    )
    pod = next(r for r in after["resources"] if r["kind"] == "Deployment")["spec"][
        "template"
    ]
    assert pod["metadata"]["annotations"].pop(ANNOTATION) == "asgi-v1"
    container = pod["spec"]["containers"][0]
    assert container["args"][-2:] == [
        "--middleware",
        "fs2_runtime_identity.RuntimeIdentityMiddleware",
    ]
    container["args"] = container["args"][:-2]
    container["env"] = container["env"][:-2]
    container["volumeMounts"].pop()
    pod["spec"]["volumes"].pop()
    after["templateDigest"] = before["templateDigest"]
    assert after == before


def test_proposal_keeps_original_resources_and_disables_old_process_snapshot():
    original = {
        "modelRef": MODEL,
        "runtime": {"image": IMAGE},
        "fastStart": {"level": "Off"},
        "cache": {"snapshotPreference": "Prefer", "snapshotRef": {"name": "old"}},
        "availability": {"minReplicas": 1, "maxReplicas": 8},
    }
    after = proposal(original, candidate_template(baseline()))
    assert after["runtime"]["image"] == IMAGE
    assert after["availability"] == original["availability"]
    assert after["cache"] == {"snapshotPreference": "Never"}
    assert "snapshotRef" in original["cache"]


def test_isolated_pods_cannot_join_production_service_and_preserve_resources():
    template = candidate_template(baseline())
    bundle = isolated(template, ["node-a", "node-b"])
    production = next(r for r in template["resources"] if r["kind"] == "Service")
    source = next(r for r in template["resources"] if r["kind"] == "Deployment")[
        "spec"
    ]["template"]["spec"]
    for pod in [r for r in bundle["items"] if r["kind"] == "Pod"]:
        assert not all(
            pod["metadata"]["labels"].get(k) == v
            for k, v in production["spec"]["selector"].items()
        )
        assert (
            pod["spec"]["containers"][0]["resources"]
            == source["containers"][0]["resources"]
        )
        assert next(
            v
            for v in pod["spec"]["containers"][0]["volumeMounts"]
            if v["name"] == "model-cache"
        )["readOnly"]
        assert pod["spec"]["activeDeadlineSeconds"] == 3600


def test_chart_adds_only_readonly_endpointslice_permission():
    chart = ROOT / "charts/control-plane/fs2-serve-control-plane"
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(chart),
            "--namespace",
            "fs2-system",
            "--set",
            "adminReadAdapters.capacity.enabled=true",
            "--set",
            "networkPolicy.kubernetesApiCidrs={192.0.2.1/32}",
            "--set",
            "image.repository=registry.nebius.cloud/unit/fs2-serve-control-plane",
            "--set",
            "image.digest=sha256:" + "1" * 64,
            "--set",
            "catalog.rolloutDigest=sha256:" + "3" * 64,
            "--set",
            "config.publicBaseUrl=https://203.0.113.17",
            "--set",
            "config.authorizationServerUrl=https://identity.unit.test",
            "--set",
            "config.publicAuthorityMode=ip",
            "--set",
            "httpRoute.authorityMode=ip",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rules = [
        rule
        for doc in yaml.safe_load_all(rendered)
        if doc and doc["kind"] == "Role"
        for rule in doc.get("rules", [])
        if "endpointslices" in rule.get("resources", [])
    ]
    assert rules == [
        {
            "apiGroups": ["discovery.k8s.io"],
            "resources": ["endpointslices"],
            "verbs": ["list"],
        }
    ]
