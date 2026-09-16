from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "charts/addons/mindeval-workshop"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required for chart rendering")


def render(tmp_path: Path, mindguard: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    values = {"workshop": {"image": "example.invalid/workshop@sha256:" + "a" * 64}}
    if mindguard is not None:
        values["mindguard"] = mindguard
    path = tmp_path / "values.json"
    path.write_text(json.dumps(values))
    assert HELM is not None
    result = subprocess.run(  # noqa: S603 - fixed local chart and test-owned values; no shell
        [HELM, "template", "workshop-test", str(CHART), "--namespace", "fs2-system", "-f", str(path)],
        text=True,
        capture_output=True,
        check=True,
    )
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def classifier_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in resources if "mindguard" in item["metadata"]["name"]]


def test_mindguard_is_disabled_without_additional_values(tmp_path: Path) -> None:
    assert classifier_resources(render(tmp_path)) == []
    assert classifier_resources(render(tmp_path, {"enabled": False})) == []


def test_chart_lock_matches_canonical_model_lock() -> None:
    assert json.loads((CHART / "files/mindguard-public-models.lock.json").read_text()) == json.loads(
        (ROOT / "models/mindguard/public-models.lock.json").read_text()
    )


def test_default_model_runtime_matches_gpu_tested_preview(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "mindguard_preview_renderer", ROOT / "models/mindguard/render_preview.py"
    )
    assert spec is not None and spec.loader is not None
    preview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preview)
    resources = classifier_resources(render(tmp_path, {"enabled": True, "existingHuggingFaceSecret": "approved-hf"}))
    assert {item["kind"] for item in resources} == {"Deployment", "Service", "PersistentVolumeClaim"}
    deployed = next(item for item in resources if item["kind"] == "Deployment")
    expected = preview.render("mindguard-4b")["items"][1]
    pod = deployed["spec"]["template"]["spec"]
    expected_pod = expected["spec"]["template"]["spec"]
    assert pod["containers"][0] == expected_pod["containers"][0]
    assert pod["securityContext"] == expected_pod["securityContext"]
    assert pod["nodeSelector"] == expected_pod["nodeSelector"]
    assert deployed["spec"]["strategy"] == {"type": "Recreate"}
    assert deployed["spec"]["replicas"] == 1
    assert deployed["metadata"]["name"] == "workshop-test-mindguard-4b"
    assert deployed["metadata"]["namespace"] == "fs2-models"
    assert pod["initContainers"][0]["env"][-1]["valueFrom"]["secretKeyRef"] == {"name": "approved-hf", "key": "token"}
    service = next(item for item in resources if item["kind"] == "Service")
    assert service["spec"]["selector"] == deployed["spec"]["selector"]["matchLabels"]
    assert service["spec"]["type"] == "ClusterIP"
    claim = next(item for item in resources if item["kind"] == "PersistentVolumeClaim")
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert claim["spec"]["accessModes"] == ["ReadWriteMany"]


def test_models_replicas_existing_cache_and_placement_are_configurable(tmp_path: Path) -> None:
    resources = classifier_resources(
        render(
            tmp_path,
            {
                "enabled": True,
                "namespace": "customer-models",
                "namePrefix": "customer",
                "existingHuggingFaceSecret": "customer-hf",
                "huggingFaceSecretKey": "hf-token",
                "cache": {"existingClaim": "approved-rwx-cache"},
                "models": {
                    "mindguard-4b": {"replicas": 0, "nodeSelector": {"kubernetes.io/hostname": "chosen-node"}},
                    "mindguard-8b": {"enabled": True, "replicas": 2},
                },
            },
        )
    )
    assert len(resources) == 4
    deployments = [item for item in resources if item["kind"] == "Deployment"]
    assert [item["spec"]["replicas"] for item in deployments] == [0, 2]
    for item in deployments:
        assert item["metadata"]["namespace"] == "customer-models"
        assert (
            item["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "approved-rwx-cache"
        )
    four, eight = [item["spec"]["template"]["spec"] for item in deployments]
    assert four["nodeSelector"]["kubernetes.io/hostname"] == "chosen-node"
    assert "kubernetes.io/hostname" not in eight["nodeSelector"]
    assert eight["containers"][0]["args"][2] == "mindguard-8b"


def test_eight_b_only_and_no_enabled_models(tmp_path: Path) -> None:
    config = {
        "enabled": True,
        "existingHuggingFaceSecret": "hf",
        "models": {"mindguard-4b": {"enabled": False}, "mindguard-8b": {"enabled": True}},
    }
    resources = classifier_resources(render(tmp_path, config))
    assert [item["metadata"]["name"] for item in resources if item["kind"] == "Deployment"] == [
        "workshop-test-mindguard-8b"
    ]
    config["models"]["mindguard-8b"]["enabled"] = False
    assert classifier_resources(render(tmp_path, config)) == []


def test_customer_cache_image_and_resource_overrides(tmp_path: Path) -> None:
    image = "example.invalid/qualified-runtime@sha256:" + "b" * 64
    resources = classifier_resources(
        render(
            tmp_path,
            {
                "enabled": True,
                "existingHuggingFaceSecret": "hf",
                "image": image,
                "imagePullSecrets": [{"name": "customer-registry"}],
                "cache": {"storageClass": "customer-rwx", "size": "128Gi", "retain": False},
                "nodeSelector": {"accelerator.fs2.nebius/class": "customer-tested-gpu"},
                "tolerations": [],
                "resources": {"requests": {"cpu": "6"}},
            },
        )
    )
    claim = next(item for item in resources if item["kind"] == "PersistentVolumeClaim")
    assert claim["spec"]["storageClassName"] == "customer-rwx"
    assert claim["spec"]["resources"]["requests"]["storage"] == "128Gi"
    assert not claim["metadata"].get("annotations")
    pod = next(item for item in resources if item["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == pod["initContainers"][0]["image"] == image
    assert pod["containers"][0]["resources"]["requests"] == {"cpu": "6", "memory": "32Gi", "nvidia.com/gpu": "1"}
    assert pod["tolerations"] == []
    assert pod["imagePullSecrets"] == [{"name": "customer-registry"}]


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"existingHuggingFaceSecret": "hf", "image": "example.invalid/runtime:latest"},
        {"existingHuggingFaceSecret": "hf", "models": {"mindguard-v2": {"enabled": True}}},
    ],
)
def test_enabled_models_require_existing_secret_and_pinned_public_identity(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        render(tmp_path, {"enabled": True, **override})
