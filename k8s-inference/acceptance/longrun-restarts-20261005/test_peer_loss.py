import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "peer_loss", Path(__file__).with_name("verify_peer_loss.py")
)
peer_loss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer_loss)


def pods():
    return [
        {
            "metadata": {
                "name": f"owned-peer-{index}",
                "uid": f"uid-{index}",
                "namespace": "fs2-models",
                "labels": {
                    "fs2.nebius.ai/operation-id": "operation",
                    "fs2.nebius.ai/attempt-id": "attempt",
                    "fs2.nebius.ai/model-id": "gromacs-mpi",
                    "jobset.sigs.k8s.io/job-index": str(index),
                },
            },
            "spec": {
                "nodeName": f"node-{index}",
                "containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}],
            },
        }
        for index in range(2)
    ]


def test_fault_targets_only_owned_nonleader_peer():
    original = pods()
    assert peer_loss.owned_peer(original, "operation", "attempt") == original[1]
    assert (
        peer_loss.owned_peer(list(reversed(original)), "operation", "attempt")
        == original[1]
    )


@pytest.mark.parametrize(
    "damage",
    [
        "operation",
        "attempt",
        "model",
        "rank",
        "node",
        "gpu",
        "namespace",
        "deleted",
        "extra",
    ],
)
def test_fault_refuses_foreign_or_ambiguous_resources(damage):
    value = pods()
    peer = value[1]
    if damage in {"operation", "attempt", "model"}:
        peer["metadata"]["labels"]["fs2.nebius.ai/" + damage + "-id"] = "someone-else"
    elif damage == "rank":
        peer["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"] = "0"
    elif damage == "node":
        peer["spec"]["nodeName"] = "node-0"
    elif damage == "gpu":
        peer["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] = "8"
    elif damage == "namespace":
        peer["metadata"]["namespace"] = "not-owned"
    elif damage == "deleted":
        peer["metadata"]["deletionTimestamp"] = "2026-10-05T16:00:00Z"
    else:
        value.append(copy.deepcopy(peer))
    with pytest.raises(ValueError):
        peer_loss.owned_peer(value, "operation", "attempt")


def test_prior_native_history_is_verified_not_just_counted():
    original = {
        "files": [
            {"path": "run.tpr", "sha256": "a" * 64, "size_bytes": 100},
            {"path": "md.part0001.xtc", "sha256": "b" * 64, "size_bytes": 200},
            {"path": "native.cpt", "sha256": "c" * 64, "size_bytes": 20},
        ]
    }
    changed = copy.deepcopy(original)
    changed["files"][2]["sha256"] = "d" * 64
    assert peer_loss.retained_files(original, changed) == 2
    changed["files"][1]["sha256"] = "e" * 64
    with pytest.raises(ValueError, match="already closed"):
        peer_loss.retained_files(original, changed)
