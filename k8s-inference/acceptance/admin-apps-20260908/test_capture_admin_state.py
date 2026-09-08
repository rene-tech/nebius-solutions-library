from capture_admin_state import pod_inventory


def test_inventory_keeps_allocation_identity_but_not_environment_or_mounts():
    pod = {
        "metadata": {
            "name": "worker",
            "namespace": "models",
            "uid": "exact-uid",
            "annotations": {"private": "value"},
        },
        "spec": {
            "nodeName": "gpu-node",
            "overhead": {"memory": "10Mi"},
            "containers": [
                {
                    "name": "runtime",
                    "resources": {"requests": {"nvidia.com/gpu": "1"}},
                    "env": [{"name": "TOKEN", "value": "not-exported"}],
                }
            ],
            "initContainers": [
                {
                    "name": "init",
                    "restartPolicy": "Always",
                    "resources": {"requests": {"cpu": "100m"}},
                }
            ],
            "volumes": [{"secret": {"secretName": "not-exported"}}],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    result = pod_inventory(pod)
    assert result["metadata"]["uid"] == "exact-uid"
    assert (
        result["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"]
        == "1"
    )
    assert result["spec"]["initContainers"][0]["restartPolicy"] == "Always"
    assert result["status"]["conditions"][0]["status"] == "True"
    assert (
        "not-exported" not in repr(result) and "annotations" not in result["metadata"]
    )


def test_inventory_retains_deletion_and_job_ownership_without_assuming_readiness():
    result = pod_inventory(
        {
            "metadata": {
                "name": "batch",
                "deletionTimestamp": "2026-09-08T12:00:00Z",
                "ownerReferences": [{"kind": "Job", "uid": "job-uid"}],
            },
            "spec": {"containers": []},
            "status": {"phase": "Pending"},
        }
    )
    assert result["metadata"]["deletionTimestamp"]
    assert result["metadata"]["ownerReferences"][0]["uid"] == "job-uid"
    assert result["status"] == {"phase": "Pending"}
