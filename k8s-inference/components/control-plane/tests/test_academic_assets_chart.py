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


def test_renderer_generates_real_subpath_mounts_from_the_bindings() -> None:
    """A claim and a mount root alone cannot produce a subPath mount."""

    config_map = academic_config_map(
        render(
            "--set",
            "academicAssets.enabled=true",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.modelId=alphafold3",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.artifactId=alphafold3-parameters",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.sourceSubPath=alphafold3/af3.bin.zst",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.consumerPath=/models/af3.bin.zst",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.mechanism=subpath-file-mount",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.contentIdentityKind=file-digest",
            "--set",
            "academicAssets.runtimeBindings.alphafold3.readOnly=true",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.modelId=bindcraft",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.artifactId=bindcraft-pyrosetta",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.sourceSubPath=pyrosetta-bindcraft/site-packages",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.consumerPath=/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.mechanism=subpath-directory-mount",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.contentIdentityKind=tree-manifest",
            "--set",
            "academicAssets.runtimeBindings.pyrosetta.readOnly=true",
        )
    )
    assert config_map is not None
    mounts = yaml.safe_load(config_map["data"]["runtime_binding_mounts"])
    by_path = {mount["mountPath"]: mount for mount in mounts["volumeMounts"]}

    assert by_path["/models/af3.bin.zst"]["subPath"] == "alphafold3/af3.bin.zst"
    assert by_path["/models/af3.bin.zst"]["readOnly"] is True
    tree = by_path["/opt/fs2/academic/pyrosetta-bindcraft/site-packages"]
    assert tree["subPath"] == "pyrosetta-bindcraft/site-packages"
    assert tree["readOnly"] is True

    assert mounts["volumes"][0]["persistentVolumeClaim"]["claimName"] == config_map["data"]["claim"]
    assert mounts["volumes"][0]["persistentVolumeClaim"]["readOnly"] is True

    published = yaml.safe_load(config_map["data"]["runtime_bindings"])
    assert published["pyrosetta"]["contentIdentityKind"] == "tree-manifest"
    assert published["alphafold3"]["contentIdentityKind"] == "file-digest"


def test_renderer_emits_no_mounts_without_bindings() -> None:
    config_map = academic_config_map(render("--set", "academicAssets.enabled=true"))
    assert config_map is not None
    mounts = yaml.safe_load(config_map["data"]["runtime_binding_mounts"])
    assert mounts["volumeMounts"] == []


BINDING_FLAGS = [
    "--set",
    "academicAssets.enabled=true",
    "--set",
    "academicAssets.execution.enabled=true",
    "--set",
    "academicAssets.runtimeBindings.af3.modelId=alphafold3",
    "--set",
    "academicAssets.runtimeBindings.af3.artifactId=alphafold3-parameters",
    "--set",
    "academicAssets.runtimeBindings.af3.sourceSubPath=alphafold3/af3.bin.zst",
    "--set",
    "academicAssets.runtimeBindings.af3.consumerPath=/models/af3.bin.zst",
    "--set",
    "academicAssets.runtimeBindings.af3.mechanism=subpath-file-mount",
    "--set",
    "academicAssets.runtimeBindings.af3.contentIdentityKind=file-digest",
    "--set",
    "academicAssets.runtimeBindings.af3.readOnly=true",
]


def execution_config_map(documents: list[dict]) -> dict | None:
    for document in documents:
        if document.get("kind") == "ConfigMap" and document["metadata"]["name"].endswith("-academic-execution"):
            return document
    return None


def test_rendered_job_runs_in_the_namespace_that_holds_the_claim() -> None:
    """A claim cannot be mounted from another namespace, so the Job must land here."""

    config_map = execution_config_map(render(*BINDING_FLAGS))
    assert config_map is not None
    data = config_map["data"]
    assert data["execution_namespace"] == data["claim_namespace"]
    assert data["cross_namespace_mount"] == "false"

    job = yaml.safe_load(data["job_template"])
    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == data["claim_namespace"]

    pod = job["spec"]["template"]["spec"]
    claim = pod["volumes"][0]["persistentVolumeClaim"]
    # Same-namespace by construction: a claimName has no namespace field.
    assert claim["claimName"] == "academic-assets-runtime-rwx"
    assert claim["readOnly"] is True
    assert pod["securityContext"]["supplementalGroups"] == [65532]
    assert pod["serviceAccountName"] == data["service_account"]
    assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == data["local_queue"]


def test_rendered_job_mounts_every_binding_by_subpath() -> None:
    config_map = execution_config_map(render(*BINDING_FLAGS))
    assert config_map is not None
    job = yaml.safe_load(config_map["data"]["job_template"])
    mounts = job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert mounts, "the rendered Job mounts nothing"
    for mount in mounts:
        assert mount["readOnly"] is True
        assert mount["subPath"]
    by_path = {mount["mountPath"]: mount for mount in mounts}
    assert by_path["/models/af3.bin.zst"]["subPath"] == "alphafold3/af3.bin.zst"


def test_no_execution_objects_without_the_feature() -> None:
    assert execution_config_map(render()) is None
    assert execution_config_map(render("--set", "academicAssets.enabled=true")) is None


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
