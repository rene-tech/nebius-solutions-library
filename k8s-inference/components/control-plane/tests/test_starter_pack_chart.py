"""Pack image is pinned, isolated from GPU controllers and disabled by default."""

import subprocess

import pytest
from test_helm_chart import render, render_command

PACK_IMAGE = "registry.nebius.cloud/unit/starter-data@sha256:" + "4" * 64
PACK_DIGEST = "5" * 64


def options():
    return [
        "--set",
        "customerStorage.enabled=true",
        "--set",
        "customerStorage.projectId=project-e00starter",
        "--set",
        "customerStorage.region=eu-north1",
        "--set",
        "customerStorage.secretName=starter-storage-provider",
        "--set",
        "customerStorage.starterPack.enabled=true",
        "--set",
        "customerStorage.starterPack.image=" + PACK_IMAGE,
        "--set",
        "customerStorage.starterPack.manifestSha256=" + PACK_DIGEST,
    ]


def test_disabled_default_has_no_pack_volume_or_initializer():
    objects = render()
    for obj in objects:
        if obj["kind"] == "Deployment":
            spec = obj["spec"]["template"]["spec"]
            assert not any(v["name"] == "starter-pack" for v in spec.get("volumes", []))
            assert not any(c["name"] == "install-starter-pack" for c in spec.get("initContainers", []))


def test_enabled_pack_is_pinned_read_only_and_has_bounded_copy_volume():
    objects = render(*options())
    deployments = [o for o in objects if o["kind"] == "Deployment"]
    installed = []
    for deployment in deployments:
        spec = deployment["spec"]["template"]["spec"]
        initializers = [i for i in spec.get("initContainers", []) if i["name"] == "install-starter-pack"]
        if not initializers:
            continue
        installed.append(deployment["metadata"]["name"])
        assert initializers[0]["image"] == PACK_IMAGE
        assert initializers[0]["securityContext"]["readOnlyRootFilesystem"] is True
        volume = next(v for v in spec["volumes"] if v["name"] == "starter-pack")
        assert volume["emptyDir"]["sizeLimit"] == "160Mi"
        runtime = next(
            c for c in spec["containers"] if any(m["name"] == "starter-pack" for m in c.get("volumeMounts", []))
        )
        assert next(m for m in runtime["volumeMounts"] if m["name"] == "starter-pack")["readOnly"] is True
        env = {e["name"]: e.get("value") for e in runtime["env"]}
        assert env["FS2_USER_STORAGE_STARTER_PACK_SHA256"] == PACK_DIGEST
        assert env["FS2_USER_STORAGE_STARTER_PACK_DIR"] == "/opt/fs2/starter-pack"
    assert installed == ["fs2-serve-control-plane"]


@pytest.mark.parametrize(
    "invalid",
    ["customerStorage.starterPack.image=registry/model:latest", "customerStorage.starterPack.manifestSha256=bad"],
)
def test_enabled_pack_requires_immutable_image_and_manifest(invalid):
    result = subprocess.run(  # noqa: S603 - fixed local Helm command and test values
        render_command(*options(), "--set", invalid), capture_output=True, text=True
    )
    assert result.returncode != 0
