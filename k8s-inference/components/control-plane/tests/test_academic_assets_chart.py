"""The chart publishes how to mount licensed academic assets, never the bytes."""

from __future__ import annotations

import shutil
import subprocess

import yaml
from conftest import SOLUTION_ROOT

CHART = SOLUTION_ROOT / "charts/control-plane/fs2-serve-control-plane"
HELM = shutil.which("helm")
assert HELM is not None

DEFAULTS = [
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
]


def render(*extra: str) -> list[dict]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only helm render
        [HELM, "template", "fs2-serve", str(CHART), "--namespace", "fs2-system", *DEFAULTS, *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def academic_config_map(documents: list[dict]) -> dict | None:
    for document in documents:
        if document.get("kind") == "ConfigMap" and document["metadata"]["name"].endswith("-academic-assets"):
            return document
    return None


def test_delivery_contract_is_absent_unless_enabled() -> None:
    assert academic_config_map(render()) is None


def test_delivery_contract_describes_a_mounted_tenant_private_volume() -> None:
    config_map = academic_config_map(render("--set", "academicAssets.enabled=true"))
    assert config_map is not None
    data = config_map["data"]
    assert data["delivery_mode"] == "tenant-private-volume"
    assert data["embeds_licensed_bytes"] == "false"
    assert data["general_shared_cache"] == "false"
    assert data["world_readable"] == "false"
    assert data["read_only"] == "true"
    assert data["consumer_access"] == "supplemental-group"
    assert data["mount_root"].startswith("/")

    contract = yaml.safe_load(data["consumer_pod_contract"])
    gid = int(data["asset_gid"])
    assert contract["securityContext"]["supplementalGroups"] == [gid]
    assert contract["volumes"][0]["persistentVolumeClaim"]["claimName"] == data["claim"]
    assert contract["volumes"][0]["persistentVolumeClaim"]["readOnly"] is True
    assert contract["volumeMounts"][0]["readOnly"] is True
    assert contract["volumeMounts"][0]["mountPath"] == data["mount_root"]


def test_no_rendered_manifest_carries_licensed_bytes() -> None:
    rendered = render("--set", "academicAssets.enabled=true")
    encoded = yaml.safe_dump_all(rendered)
    for forbidden in ("af3.bin", ".whl", "BEGIN PRIVATE KEY", "BEGIN RSA"):
        assert forbidden not in encoded


def test_control_plane_pods_never_mount_the_licensed_volume() -> None:
    """The API server has no reason to hold model weights."""

    rendered = render("--set", "academicAssets.enabled=true")
    for document in rendered:
        if document.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        volumes = document["spec"]["template"]["spec"].get("volumes") or []
        for volume in volumes:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            assert claim != "academic-assets-runtime-rwx"
