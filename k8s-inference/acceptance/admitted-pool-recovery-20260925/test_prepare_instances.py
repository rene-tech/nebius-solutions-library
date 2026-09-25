"""Disjoint exact task instances must not share server or admission identities."""
import base64
import json
import subprocess

import pytest

import admission_injector as injector
import prepare_injector as render


def config(instance):
    return {
        "tenant": injector.TENANT, "namespace": injector.NAMESPACE,
        "dead_pool": "h100-1x", "shard": "window-03" if instance == "init" else "window-01",
        "runtime_image": "registry/gromacs@sha256:" + "1" * 64,
        "pause_seconds": 150 if instance == "init" else 0,
        "synthetic_no_capacity": instance == "synthetic",
        "expires_at": "2026-09-25T11:00:00+00:00",
    }


@pytest.mark.parametrize("instance", ["recovery", "init", "synthetic"])
def test_instance_references_and_matchers_are_exact(instance):
    name, namespace = render.instance_identity(instance)
    resources = render.bundle(config(instance), render.SERVER_IMAGE, "source", b"ca", b"cert", b"key", instance=instance)
    assert resources["namespace.json"]["metadata"]["name"] == namespace
    items = resources["server.json"]["items"]
    assert all(item["metadata"]["namespace"] == namespace for item in items)
    assert all(item["metadata"]["labels"][render.OWNER] == injector.TENANT for item in items)
    deployment = next(item for item in items if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == name
    assert pod["volumes"][0]["configMap"]["name"] == name + "-code"
    assert pod["volumes"][1]["secret"]["secretName"] == name + "-tls"
    webhook = resources["webhook.json"]["webhooks"][0]
    assert resources["webhook.json"]["metadata"]["name"] == name
    assert webhook["clientConfig"]["service"] == {"name": name, "namespace": namespace, "port": 443, "path": "/mutate"}
    assert webhook["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "fs2-models"}
    selector = webhook["objectSelector"]["matchLabels"]
    assert selector == {injector.PREFIX + "tenant-id": injector.TENANT, injector.PREFIX + "model-id": "gromacs",
                        injector.PREFIX + "stage-id": "workflow", injector.PREFIX + "shard-id": config(instance)["shard"]}
    assert base64.b64decode(webhook["clientConfig"]["caBundle"]) == b"ca"
    assert json.loads(next(item for item in items if item["kind"] == "ConfigMap")["data"]["config.json"]) == config(instance)


def test_parallel_cases_have_different_resource_and_shard_identities():
    assert len({render.instance_identity(instance)[0] for instance in ("recovery", "init", "synthetic")}) == 3
    assert len({render.instance_identity(instance)[1] for instance in ("recovery", "init", "synthetic")}) == 3
    assert config("init")["shard"] != config("synthetic")["shard"]
    assert render.instance_identity("recovery") == (render.NAME, render.SERVER_NAMESPACE)
    with pytest.raises(injector.NotEligible, match="unknown_injector_instance"):
        render.instance_identity("customer")


@pytest.mark.parametrize("instance", ["init", "synthetic"])
def test_instance_certificates_verify_exact_service_dns(tmp_path, instance):
    render.certificates(tmp_path, instance)
    name, namespace = render.instance_identity(instance)
    result = subprocess.run(["openssl", "verify", "-CAfile", str(tmp_path / "ca.crt"), "-purpose", "sslserver",
                             "-verify_hostname", name + "." + namespace + ".svc", str(tmp_path / "tls.crt")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
