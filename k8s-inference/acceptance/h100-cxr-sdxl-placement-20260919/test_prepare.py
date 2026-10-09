"""Metadata join regression tests, not substitute scientific qualification."""

import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("h100_placement_proof", Path(__file__).with_name("prepare.py"))
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


@pytest.fixture
def evidence():
    digest = "sha256:" + "a" * 64
    record = {"runtime": {"image": {"digest": digest}}, "model": {"source": {"revision": "revision"}}}
    operation = {
        "started_at": "2026-09-19T00:00:01Z",
        "runtime": {"pod_uid": "pod-uid", "node_uid": "node-uid", "gpu_count": 1, "gpu_uuids": ["GPU-exact"]},
    }
    node = {
        "metadata": {
            "name": "node",
            "uid": "node-uid",
            "labels": {
                "accelerator.fs2.nebius/class": prepare.CLASS,
                "node.kubernetes.io/instance-type": "gpu-h100-sxm",
                "nebius.com/nvidia_driver_version": "580.159.04-1ubuntu1",
                "accelerator.fs2.nebius/pool-id": "h100-1x",
            },
        }
    }
    pod = {
        "metadata": {
            "uid": "pod-uid",
            "annotations": {
                prepare.GPU_IDS: '["GPU-exact"]',
                "fs2.nebius/runtime-image-digest": digest,
                "fs2.nebius/model-revision": "revision",
                "fs2.nebius/model-content-digest": "sha256:content",
            },
        },
        "spec": {
            "nodeName": "node",
            "containers": [
                {"name": "model", "image": "registry/image@" + digest, "resources": {"limits": {"nvidia.com/gpu": 1}}}
            ],
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "model",
                    "imageID": "registry/image@" + digest,
                    "containerID": "containerd://same",
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-09-19T00:00:00Z"}},
                }
            ]
        },
    }
    return pod, node, operation, record, "content"


def test_exact_runtime_identity_witness(evidence):
    assert prepare.witness(*evidence)["gpu_uuids"] == ["GPU-exact"]


@pytest.mark.parametrize("fault", ["sku", "gpu_uuid", "pod_uid", "node_uid", "actual_image", "content", "restarted"])
def test_inexact_or_restarted_runtime_cannot_prove_placement(evidence, fault):
    pod, node, operation, record, digest = copy.deepcopy(evidence)
    if fault == "sku":
        node["metadata"]["labels"]["node.kubernetes.io/instance-type"] = "gpu-l40s"
    elif fault == "gpu_uuid":
        operation["runtime"]["gpu_uuids"] = ["GPU-other"]
    elif fault == "pod_uid":
        pod["metadata"]["uid"] = "different-pod"
    elif fault == "node_uid":
        node["metadata"]["uid"] = "different-node"
    elif fault == "actual_image":
        pod["status"]["containerStatuses"][0]["imageID"] = "registry/image@sha256:" + "b" * 64
    elif fault == "content":
        pod["metadata"]["annotations"]["fs2.nebius/model-content-digest"] = "sha256:different"
    else:
        pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = "2026-09-19T00:00:02Z"
    with pytest.raises(ValueError, match="witness mismatch"):
        prepare.witness(pod, node, operation, record, digest)
