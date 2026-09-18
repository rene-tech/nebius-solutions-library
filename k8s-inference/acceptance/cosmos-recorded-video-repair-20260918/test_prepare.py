import copy
import json

import prepare as p
import pytest
import yaml


def bundle():
    docs = list(yaml.safe_load_all((p.ROOT / "models/general-media/k8s/cosmos3-nano.yaml").read_text()))
    return {
        "modelRef": p.MODEL,
        "runtimeProfile": "vllm-omni",
        "templateDigest": "sha256:" + "a" * 64,
        "primaryWorkloadName": p.MODEL,
        "runtimeContainerName": "vllm-omni",
        "primaryServiceName": p.MODEL,
        "primaryServicePort": 8080,
        "resources": [d for d in docs if d["kind"] in {"ServiceAccount", "ConfigMap", "Deployment", "Service"}],
    }


def test_template_versions_adapter_without_modifying_settings_or_history():
    before = bundle()
    original = copy.deepcopy(before)
    after = p.template(before)
    assert before == original and after["templateDigest"] != before["templateDigest"]
    old_deployment = next(r for r in before["resources"] if r["kind"] == "Deployment")
    deployment = next(r for r in after["resources"] if r["kind"] == "Deployment")
    old_pod, pod = old_deployment["spec"]["template"]["spec"], deployment["spec"]["template"]["spec"]
    for old, new in zip(
        [*old_pod["containers"], *old_pod["initContainers"]], [*pod["containers"], *pod["initContainers"]], strict=True
    ):
        assert new["image"] == p.NEW
        new["image"] = old["image"]
        assert new == old
    adapter = next(r for r in after["resources"] if r["kind"] == "ConfigMap")
    assert adapter["metadata"]["name"].startswith("cosmos3-nano-adapter-")
    assert next(v["configMap"]["name"] for v in pod["volumes"] if v["name"] == "adapter") == adapter["metadata"]["name"]


def fixture():
    base = p.shared.catalog().model(p.MODEL).to_dict()
    row = p.shared.read(p.ROOT / "catalog/runtime/deployment-runtimes/genmol-portable-h100.json")["qualification"]
    row.update(model_id=p.MODEL, variant_id=None)
    row["active_runtime"] = {
        "model_revision": base["model"]["source"]["revision"],
        "runtime_image_digest": base["runtime"]["image"]["digest"],
        "service": {"namespace": "fs2-models", "name": p.MODEL, "port": 8080},
    }
    from fs2_serve.qualification import _policy

    row["policy"] = _policy(p.shared.catalog(), p.MODEL)
    entry = p.successor(base, row, {"test_fixture_only": True})
    previous = bundle()
    digest = previous["templateDigest"]
    owner = {
        "metadata": {"name": p.MODEL, "namespace": "fs2-models"},
        "spec": {
            "modelRef": p.MODEL,
            "runtime": {
                "image": base["runtime"]["image"]["reference"],
                "templateRef": {"name": "old", "digest": digest},
            },
            "cache": {"tier": "SharedFilesystem", "snapshotPreference": "Prefer", "snapshotRef": {"name": "old-r7"}},
            "fastStart": {"level": "Off", "mode": "Fixed"},
            "availability": {"maxReplicas": 4, "minReplicas": 0},
            "placement": {"poolRefs": ["h100-1x", "h100-reserved-8x"]},
            "future_option": {"keep": True},
        },
    }
    envelope = {
        "revision": "old",
        "qualifications": {
            p.MODEL: {
                "runtimeImages": [owner["spec"]["runtime"]["image"]],
                "templateDigests": [digest],
                "templateRefs": {"old": digest},
                "templateCacheTiers": {digest: "SharedFilesystem"},
                "gpuSnapshotBundles": {"old-r7": {"historical": True}},
            },
            "unrelated-app": {"future": "preserved"},
        },
    }
    routes = {
        "deployment-runtimes.json": json.dumps({"models": {}}),
        "qualification-projection.json": json.dumps({"rows": [row]}),
        "lean-routes.json": "unchanged",
    }
    contract = p.existing.catalog_configuration_contracts(p.shared.catalog(), deployment_runtime_entries={})[p.MODEL]
    admin = {
        "models": {
            p.MODEL: {
                "artifact": {field: getattr(contract, attribute) for field, attribute in p.shared.IDENTITIES.items()},
                "settings": {"keep": True},
            }
        }
    }
    return envelope, [previous], routes, admin, [owner], entry


def test_successor_disables_only_restore_and_preserves_customer_knobs_and_siblings():
    values = fixture()
    original = copy.deepcopy(values)
    envelope, bundles, routes, admin, proposals = p.extend(*values)
    assert values == original and bundles[:-1] == values[1]
    assert envelope["qualifications"]["unrelated-app"] == {"future": "preserved"}
    assert envelope["qualifications"][p.MODEL]["gpuSnapshotBundles"] == {"old-r7": {"historical": True}}
    spec, old = proposals[0]["spec"], original[4][0]["spec"]
    assert spec["cache"] == {"tier": "SharedFilesystem", "snapshotPreference": "Never"}
    assert spec["availability"] == old["availability"] and spec["placement"] == old["placement"]
    assert spec["future_option"] == old["future_option"]
    assert routes["lean-routes.json"] == "unchanged"
    assert admin["models"][p.MODEL]["settings"] == {"keep": True}
    selected = json.loads(routes["deployment-runtimes.json"])["models"][p.MODEL]
    assert selected["variant_id"] is None
    assert len(selected["qualification"]["evidence"]["retained_deployments_sha256"]) == 64
    assert not selected["qualification"]["states"]["http_mcp_qualified"]


@pytest.mark.parametrize(
    ("field", "value"), [("width", 736), ("height", 544), ("nb_read_frames", "65"), ("avg_frame_rate", "24/1")]
)
def test_media_evidence_rejects_actual_wrong_shape_rate_or_frame_count(field, value):
    stream = {"codec_type": "video", "width": 640, "height": 480, "nb_read_frames": "64", "avg_frame_rate": "25/1"}
    request = {"size": "640x480", "num_frames": 64, "fps": 25}
    p.media_check({"streams": [stream]}, request)
    with pytest.raises(ValueError, match="differs from request"):
        p.media_check({"streams": [{**stream, field: value}]}, request)


def test_unexpected_template_cannot_be_silently_rewritten():
    previous = bundle()
    next(r for r in previous["resources"] if r["kind"] == "ConfigMap")["metadata"]["name"] = "unexpected"
    with pytest.raises(ValueError, match="exact Cosmos adapter"):
        p.template(previous)
