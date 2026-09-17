from render_media_preview import NAME, ROOT, RUNTIME_IMAGE, render


def test_preview_is_isolated_and_cannot_write_shared_cache():
    config, deployment = render("computeinstance-task-owned")["items"]
    assert config["metadata"]["name"] == NAME and config["immutable"]
    assert deployment["spec"]["selector"] == {"matchLabels": {"app.kubernetes.io/instance": NAME}}
    template = deployment["spec"]["template"]
    assert "fs2-serve.nebius.ai/model-deployment" not in template["metadata"]["labels"]
    spec = template["spec"]
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "computeinstance-task-owned"
    assert not spec["automountServiceAccountToken"]
    assert next(v for v in spec["volumes"] if v["name"] == "model-cache")["persistentVolumeClaim"]["readOnly"]
    for container in [*spec["initContainers"], *spec["containers"]]:
        assert container["image"] == RUNTIME_IMAGE
        for mount in container["volumeMounts"]:
            if mount["name"] == "model-cache":
                assert mount["readOnly"]
    runtime = next(c for c in spec["containers"] if c["name"] == "vllm-omni")
    assert runtime["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert runtime["command"] == ["vllm"]
    assert {"mountPath": "/dev/shm", "name": "runtime-shm"} in runtime["volumeMounts"]  # noqa: S108 - pod-local bounded tmpfs
    assert next(v for v in spec["volumes"] if v["name"] == "runtime-shm")["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "2Gi",
    }


def test_snapshot_preview_preserves_new_media_mounts_and_cannot_fallback():
    bundle = ROOT / "acceptance/h100-fleet/snapshots/cosmos3-nano-bundle.json"
    _, deployment = render(
        "computeinstance-task-owned",
        bundle,
        name="cosmos3-nano-media-snapshot-r20260917",
        pool="h100-reserved-8x",
        capacity_type="regular",
    )["items"]
    spec = deployment["spec"]["template"]["spec"]
    runtime = next(c for c in spec["containers"] if c["name"] == "vllm-omni")
    adapter = next(c for c in spec["containers"] if c["name"] == "bounded-json-adapter")
    command = runtime["command"]
    assert command[command.index("--fallback") + 1] == "fail"
    for container in (runtime, adapter):
        assert {"name": "cosmos-control-tmp", "mountPath": "/cosmos-control-tmp"} in container["volumeMounts"]
    assert next(v for v in spec["volumes"] if v["name"] == "snapshot-bundle")["persistentVolumeClaim"]["readOnly"]
    assert next(c for c in spec["initContainers"] if c["name"] == "verify-existing-model")
