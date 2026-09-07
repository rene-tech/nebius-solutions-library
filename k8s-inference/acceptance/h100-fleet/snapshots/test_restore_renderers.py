import copy
import hashlib
import json
from pathlib import Path

from render_readonly_server_restore import render as server_restore
from render_scientific_restore import scientific_restore


HERE = Path(__file__).resolve().parent


def source_job():
    return {
        "metadata": {"namespace": "models"},
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": {"pool": "heterogeneous-h100"},
                    "tolerations": [{"key": "dedicated", "operator": "Exists"}],
                    "containers": [
                        {
                            "name": "scientific-stage",
                            "image": "model@sha256:" + "a" * 64,
                            "command": ["python", "/workspace/stage-runner.py"],
                            "args": ["--", "original-wrapper", "--steps", "200"],
                            "env": [{"name": "FS2_ORIGINAL", "value": "identity"}],
                            "resources": {
                                "requests": {
                                    "nvidia.com/gpu": "1",
                                    "cpu": "8",
                                    "memory": "96Gi",
                                },
                                "limits": {
                                    "nvidia.com/gpu": "1",
                                    "cpu": "32",
                                    "memory": "256Gi",
                                },
                            },
                            "volumeMounts": [
                                {
                                    "name": "model",
                                    "mountPath": "/models",
                                    "readOnly": True,
                                }
                            ],
                        },
                        {
                            "name": "collector",
                            "image": "tools@sha256:" + "b" * 64,
                            "command": ["collect-original"],
                        },
                    ],
                    "initContainers": [
                        {
                            "name": "verify-original",
                            "image": "tools@sha256:" + "b" * 64,
                            "command": ["verify-original"],
                            "volumeMounts": [],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "model",
                            "persistentVolumeClaim": {
                                "claimName": "original-model-cache"
                            },
                        }
                    ],
                }
            }
        },
    }


def config():
    bundle = json.loads((HERE / "protenix-v2-bundle.json").read_bytes())
    return {**bundle, "name": "isolated-restore"}


def test_scientific_restore_preserves_original_execution_and_resources():
    source = source_job()
    before = copy.deepcopy(source)
    result = scientific_restore(source, config())
    original = source["spec"]["template"]["spec"]
    spec = result["spec"]
    main = spec["containers"][0]
    assert source == before
    assert main["image"] == original["containers"][0]["image"]
    assert main["resources"] == original["containers"][0]["resources"]
    assert (
        main["command"][main["command"].index("--") + 1 :]
        == original["containers"][0]["command"] + original["containers"][0]["args"]
    )
    assert spec["nodeSelector"] == original["nodeSelector"]
    assert spec["tolerations"] == original["tolerations"]
    assert spec["containers"][1] == original["containers"][1]
    assert spec["initContainers"][1:] == original["initContainers"]
    identity_index = main["command"].index("--request-uid")
    assert ["--request-uid", "10001", "--request-gid", "10001"] == main["command"][
        identity_index : identity_index + 4
    ]
    assert next(v for v in spec["volumes"] if v["name"] == "snapshot-checkpoints") == {
        "name": "snapshot-checkpoints",
        "emptyDir": {},
    }
    assert all(
        m["readOnly"]
        for m in main["volumeMounts"]
        if m["name"] in {"snapshot-bundle", "snapshot-cli"}
    )
    assert not any(spec.get(key) for key in ("hostPID", "hostNetwork"))
    assert "privileged" not in main["securityContext"]


def test_server_restore_keeps_original_sidecar_and_private_cache():
    donor = scientific_restore(source_job(), config())
    runtime = donor["spec"]["containers"][0]
    # Convert the synthetic renderer result back to a simple donor specimen.
    runtime["command"] = [
        "python",
        "/snapshot-source/serving_supervisor.py",
        "--directory",
        "/checkpoints/run",
        "donor",
        "--",
        "vllm",
        "serve",
        "model",
    ]
    donor["spec"]["volumes"] = [
        v
        for v in donor["spec"]["volumes"]
        if v["name"] not in {"snapshot-bundle", "snapshot-cli"}
    ]
    next(
        v for v in donor["spec"]["volumes"] if v["name"] == "snapshot-checkpoints"
    ).update(persistentVolumeClaim={"claimName": "captured"})
    before = copy.deepcopy(donor)
    result = server_restore(
        donor,
        name="fresh",
        container="scientific-stage",
        network_configmap="nft-helper",
    )
    assert donor == before
    assert result["spec"]["containers"][1] == donor["spec"]["containers"][1]
    assert result["spec"]["containers"][0]["resources"] == runtime["resources"]
    initializer = result["spec"]["initContainers"][0]
    assert "xtables-nft-multi" in initializer["command"][2]
    assert "runtime-cache tmp" in initializer["command"][2]
    assert "donor" not in result["spec"]["containers"][0]["command"]


def test_captured_socket_address_is_added_only_to_private_pod_namespace():
    donor = scientific_restore(source_job(), config())
    donor["status"] = {"podIP": "10.20.30.40"}
    runtime = donor["spec"]["containers"][0]
    runtime["command"] = [
        "python",
        "/snapshot-source/serving_supervisor.py",
        "--directory",
        "/checkpoints/run",
        "donor",
        "--",
        "vllm",
        "serve",
        "model",
    ]
    next(
        v for v in donor["spec"]["volumes"] if v["name"] == "snapshot-checkpoints"
    ).update(persistentVolumeClaim={"claimName": "captured"})
    result = server_restore(
        donor,
        name="fresh",
        container="scientific-stage",
        address_configmap="address-source",
    )
    initializer = result["spec"]["initContainers"][0]
    assert initializer["command"][-1] == "10.20.30.40"
    assert initializer["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["NET_ADMIN"],
    }
    assert "nvidia.com/gpu" not in initializer["resources"]["requests"]
    assert not result["spec"].get("hostNetwork")
    assert initializer["command"][0] == "python3"
    explicit_default = server_restore(
        donor, name="fresh", container="scientific-stage",
        address_configmap="address-source", address_python="python3",
    )
    assert explicit_default == result
    interpreter = "/opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3"
    alternate = server_restore(
        donor, name="fresh", container="scientific-stage",
        address_configmap="address-source", address_python=interpreter,
    )
    expected = copy.deepcopy(result)
    expected["spec"]["initContainers"][0]["command"][0] = interpreter
    assert alternate == expected


def test_qualified_bundle_matches_source_and_receipt_bytes():
    bundle = config()
    source = HERE.parents[2] / "models/scientific-snapshot"
    for name, expected in bundle["source_sha256"].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected
    assert (
        hashlib.sha256((source / "protenix_cli_proxy.py").read_bytes()).hexdigest()
        == bundle["cli_sha256"]
    )
    assert (
        hashlib.sha256(
            (HERE / "protenix-v2-h100-20260907.json").read_bytes()
        ).hexdigest()
        == bundle["qualification_receipt_sha256"]
    )
    report = json.loads((HERE / "protenix-v2-h100-20260907.json").read_bytes())
    assert len(report["runs"]) == 6
    assert report["bundle"]["sha256"] == bundle["manifest_sha256"]
    assert all(
        run["normal_model_loader_seconds"] is None
        for run in report["runs"]
        if run["mode"] == "restore"
    )
