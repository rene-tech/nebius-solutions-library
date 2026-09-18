import json

import pytest

from prepare import CASES, SOURCE, prepare


def original():
    artifacts = [{"artifact_id": name, "mount_path": mount, "content_digest": "sha256:"+char*64,
                  "localization_receipt_digest": "sha256:"+char*64} for name, mount, char in
                 (("openfold3-openbind-0", "/models/openfold3", "a"),
                  ("openfold3-components-bcif", "/databases/openfold3", "b"))]
    return {"spec": {"nodeSelector": {"accelerator.fs2.nebius/pool-id": "original-pool"},
            "securityContext": {"runAsNonRoot": True}, "volumes": [{"name": "public", "hostPath": {"path": "/original/data"}}],
            "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1", "memory": "96Gi"},
                                         "limits": {"nvidia.com/gpu": "1", "memory": "256Gi"}},
                            "securityContext": {"runAsUser": 10001, "allowPrivilegeEscalation": False},
                            "env": [{"name": "FS2_RUNTIME_ARTIFACTS_JSON", "value": json.dumps({"artifacts": artifacts, "variant_id": "upstream-openbind-v0-5-0"})}],
                            "volumeMounts": [{"name": "public", "mountPath": a["mount_path"], "subPath": a["artifact_id"], "readOnly": True} for a in artifacts]}]}}


def fixtures(tmp_path):
    for pdb, seed in CASES:
        folder = tmp_path / "references" / pdb
        folder.mkdir(parents=True)
        (folder / f"openfold3-openbind-{pdb}-heteromer-s{seed}-input.json").write_text(
            json.dumps({"queries": {pdb: {"chains": [{"sequence": "ACDE"}, {"sequence": "FGHI"}]}}}))
    return tmp_path


def test_preserves_original_envelope_and_default_shm(tmp_path):
    source = original()
    data = prepare(source, fixtures(tmp_path / "fixtures"), "registry/image@sha256:"+"c"*64, tmp_path / "output", "test-inline")
    config, pod = data["items"]
    assert pod["kind"] == "Pod" and len(data["items"]) == 2
    assert pod["spec"]["nodeSelector"] == source["spec"]["nodeSelector"]
    assert pod["spec"]["preemptionPolicy"] == "Never"
    container = pod["spec"]["containers"][0]
    assert container["resources"] == source["spec"]["containers"][0]["resources"]
    assert container["securityContext"] == source["spec"]["containers"][0]["securityContext"]
    assert not any(m["mountPath"] == "/dev/shm" for m in container["volumeMounts"])
    assert "67108864" in container["args"][-1]
    assert not any(v.get("emptyDir", {}).get("medium") == "Memory" for v in pod["spec"]["volumes"])
    assert pod["metadata"]["annotations"]["fs2.nebius.ai/source-commit"] == SOURCE
    assert config["metadata"]["name"] == "test-inline-input"
    for pdb, seed in CASES:
        assert f"{pdb}-s{seed}.json" in config["data"]
        assert f"--model-seeds {seed}" in container["args"][-1]
        assert f"CONFIG_ACCEPTED {pdb}-s{seed}" in container["args"][-1]
    assert "InferenceExperimentConfig.model_validate" in container["args"][-1]
    assert "ALL_CASES_COMPLETE" in container["args"][-1]


def test_mutable_image_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="pinned"):
        prepare(original(), fixtures(tmp_path / "fixtures"), "registry/image:latest", tmp_path / "output", "test-inline")


def test_writable_artifact_rejected(tmp_path):
    source = original()
    source["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] = False
    with pytest.raises(ValueError, match="read-only"):
        prepare(source, fixtures(tmp_path / "fixtures"), "registry/image@sha256:"+"c"*64, tmp_path / "output", "test-inline")
