import copy

import pytest

from activate_bridge import IMAGE, OLD
from verify_bridge import readers


def fixture():
    deployment = {"metadata": {"uid": "deployment"}, "spec": {"replicas": 2}}
    sets = [{"metadata": {"uid": "set", "ownerReferences": [{"uid": "deployment", "controller": True}]}}]
    pod = {"metadata": {"uid": "pod", "ownerReferences": [{"uid": "set", "controller": True}]},
           "spec": {"containers": [{"name": "control-plane", "image": IMAGE}],
                    "initContainers": [{"name": "wait-schema", "image": IMAGE}]},
           "status": {"conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"restartCount": 0}]}}
    return deployment, sets, [pod, copy.deepcopy(pod)]


def test_exact_current_readers_pass_without_mutation():
    deployment, sets, pods = fixture()
    assert readers(deployment, sets, pods, 2) == pods


@pytest.mark.parametrize("failure", ["old", "old-init", "extra", "terminating", "unready", "restart", "hpa"])
def test_reader_barrier_rejects_mixed_or_incomplete_release(failure):
    deployment, sets, pods = fixture()
    if failure == "old":
        pods[0]["spec"]["containers"][0]["image"] = OLD
    elif failure == "old-init":
        pods[0]["spec"]["initContainers"][0]["image"] = OLD
    elif failure == "extra":
        pods.append(copy.deepcopy(pods[0]))
    elif failure == "terminating":
        pods[0]["metadata"]["deletionTimestamp"] = "2026-10-05T19:16:00Z"
    elif failure == "unready":
        pods[0]["status"]["conditions"][0]["status"] = "False"
    elif failure == "restart":
        pods[0]["status"]["containerStatuses"][0]["restartCount"] = 1
    else:
        deployment["spec"]["replicas"] = 3
    with pytest.raises(ValueError):
        readers(deployment, sets, pods, 2)
