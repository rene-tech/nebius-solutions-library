from __future__ import annotations

import shutil
import subprocess

import yaml
from conftest import SOLUTION_ROOT

CHART = SOLUTION_ROOT / "charts/control-plane/fs2-serve-control-plane"
HELM = shutil.which("helm")
assert HELM is not None


def command(*extra: str) -> list[str]:
    return [
        HELM,
        "template",
        "fs2-serve",
        str(CHART),
        "--namespace",
        "fs2-system",
        "--set",
        "image.repository=registry.example/unit/control-plane",
        "--set",
        f"image.digest=sha256:{'1' * 64}",
        "--set",
        f"catalog.rolloutDigest=sha256:{'3' * 64}",
        "--set",
        "config.publicBaseUrl=https://203.0.113.17",
        "--set",
        "config.authorizationServerUrl=https://identity.unit.test",
        "--set",
        "config.publicAuthorityMode=ip",
        "--set",
        "httpRoute.authorityMode=ip",
        *extra,
    ]


def render(*extra: str) -> tuple[str, list[dict]]:
    result = subprocess.run(command(*extra), check=True, capture_output=True, text=True)  # noqa: S603
    return result.stdout, [document for document in yaml.safe_load_all(result.stdout) if document]


def test_bootstrap_access_hook_uses_only_the_terraform_secret_reference() -> None:
    raw, documents = render(
        "--set",
        "bootstrapAccess.enabled=true",
        "--set",
        "bootstrapAccess.secretName=fs2-serve-bootstrap-access",
        "--set",
        "bootstrapAccess.tenantId=tenant-a",
    )

    job = next(
        document
        for document in documents
        if document["kind"] == "Job" and document["metadata"]["name"].endswith("-bootstrap-access")
    )
    assert job["metadata"]["annotations"] == {
        "helm.sh/hook": "post-install,post-upgrade",
        "helm.sh/hook-weight": "5",
        "helm.sh/hook-delete-policy": "before-hook-creation,hook-succeeded",
    }
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"].endswith("-bootstrap")
    assert pod["containers"][0]["args"] == ["bootstrap-access"]
    environment = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert environment["FS2_BOOTSTRAP_ACCESS_TOKEN_FILE"]["value"].endswith("/bootstrap-access-token")
    assert environment["FS2_BOOTSTRAP_ACCESS_SCOPES"]["value"]
    secret_volume = next(item for item in pod["volumes"] if item["name"] == "bootstrap-access-token")
    assert secret_volume["secret"]["secretName"] == "fs2-serve-bootstrap-access"
    assert all(document["kind"] != "Secret" for document in documents)
    assert "fs2_pat_" not in raw
    assert any(
        document["kind"] == "NetworkPolicy"
        and document["metadata"]["name"].endswith("-bootstrap-access")
        and document["spec"]["ingress"] == []
        for document in documents
    )


def test_bootstrap_access_resources_are_absent_when_disabled() -> None:
    _, documents = render()
    assert not any(document["metadata"]["name"].endswith("-bootstrap-access") for document in documents)
    assert not any(
        document["metadata"]["name"].endswith("-bootstrap-scientific-access")
        for document in documents
    )


def test_academic_scientific_access_is_a_distinct_tenant_bound_pat() -> None:
    raw, documents = render(
        "--set",
        "bootstrapAccess.enabled=true",
        "--set",
        "bootstrapAccess.secretName=fs2-serve-bootstrap-access",
        "--set",
        "bootstrapAccess.tenantId=tenant-general",
        "--set",
        "scientificAccess.enabled=true",
        "--set",
        "scientificAccess.secretName=fs2-serve-scientific-access",
        "--set",
        "scientificAccess.tenantId=tenant-academic",
    )

    jobs = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Job"
        and "bootstrap" in document["metadata"]["name"]
    }
    general = next(job for name, job in jobs.items() if name.endswith("-bootstrap-access"))
    scientific = next(
        job
        for name, job in jobs.items()
        if name.endswith("-bootstrap-scientific-access")
    )
    general_env = {
        item["name"]: item
        for item in general["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    scientific_pod = scientific["spec"]["template"]["spec"]
    scientific_env = {
        item["name"]: item for item in scientific_pod["containers"][0]["env"]
    }

    assert general_env["FS2_BOOTSTRAP_ACCESS_TENANT_ID"]["value"] == "tenant-general"
    assert scientific_env["FS2_BOOTSTRAP_ACCESS_TENANT_ID"]["value"] == "tenant-academic"
    assert scientific_env["FS2_BOOTSTRAP_ACCESS_TOKEN_FILE"]["value"].endswith(
        "/scientific-access-token"
    )
    assert {item["secret"]["secretName"] for item in scientific_pod["volumes"] if "secret" in item} >= {
        "fs2-serve-scientific-access"
    }
    assert scientific["metadata"]["annotations"]["helm.sh/hook-weight"] == "6"
    assert all(document["kind"] != "Secret" for document in documents)
    assert "fs2_pat_" not in raw
    assert any(
        document["kind"] == "NetworkPolicy"
        and document["metadata"]["name"].endswith("-bootstrap-scientific-access")
        and document["spec"]["ingress"] == []
        for document in documents
    )
