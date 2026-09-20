"""Static safety/identity checks for the isolated managed runtime."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ITEMS = json.loads((ROOT / "runtime-20260920.json").read_text())["items"]


def item(kind):
    return next(value for value in ITEMS if value["kind"] == kind)


def test_private_zero_replica_staging_has_one_gpu_and_exact_images():
    workload = item("Deployment")
    assert workload["spec"]["replicas"] == 0
    pod = workload["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["kubernetes.io/hostname"] == "computeinstance-e00jqs4xxntre6eycf"
    assert pod["automountServiceAccountToken"] is False
    nim, adapter = pod["containers"]
    assert nim["image"].endswith("@sha256:1891a2421b57cd5f2249f0b44a2720bbca24804e8e579af297a90c876d62659f")
    assert adapter["image"].endswith("@sha256:1a14cccb640d08ae5775f6ebcb9e2fe766f08b717e43526e30057a8c68edfadd")
    assert nim["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert "nvidia.com/gpu" not in adapter["resources"]["limits"]
    assert adapter["securityContext"]["readOnlyRootFilesystem"] is True
    assert all(c["securityContext"]["allowPrivilegeEscalation"] is False for c in pod["containers"])


def test_secret_reference_and_private_adapter_boundary():
    pod = item("Deployment")["spec"]["template"]["spec"]
    nim, adapter = pod["containers"]
    values = {value["name"]: value for value in nim["env"]}
    assert values["NGC_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "wan2-ngc-api", "key": "NGC_API_KEY"}
    assert "value" not in values["NGC_API_KEY"]
    assert values["NIM_HTTP_API_PORT"]["value"] == "8001"
    assert adapter["env"] == [{"name": "COSMOS_TRANSFER_NIM_URL", "value": "http://127.0.0.1:8001"}]
    service = item("Service")["spec"]
    assert service["type"] == "ClusterIP"
    assert service["ports"] == [{"name": "http", "port": 8000, "targetPort": "http"}]
    assert item("NetworkPolicy")["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8000}]


def test_dedicated_retained_cache_is_not_a_sibling_cache():
    pvc = item("PersistentVolumeClaim")
    assert pvc["metadata"]["name"] == "cosmos-transfer2-5-2b-cache"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "96Gi"
    pod = item("Deployment")["spec"]["template"]["spec"]
    assert pod["volumes"][0] == {"name": "cache", "persistentVolumeClaim": {"claimName": pvc["metadata"]["name"]}}
    assert pod["imagePullSecrets"] == [{"name": "wan2-ngc-pull"}]
