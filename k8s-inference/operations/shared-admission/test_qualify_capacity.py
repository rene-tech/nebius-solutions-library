import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "qualify_capacity", Path(__file__).with_name("qualify_capacity.py")
)
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


@pytest.mark.parametrize("shape", ["routine", "atlas"])
@pytest.mark.parametrize("concurrency", [4, 8])
def test_plan_preserves_full_shape_and_queues_one_extra_per_lane(
    tmp_path, monkeypatch, shape, concurrency
):
    pools = ("l40s-4x", "l40s-1x", "h100-ondemand-1x", "h100-reserved-8x")
    baseline = {
        "spec": {
            "cohortName": "production",
            "resourceGroups": [
                {
                    "coveredResources": ["nvidia.com/gpu"],
                    "flavors": [
                        {
                            "name": "inference-" + pool,
                            "resources": [
                                {
                                    "name": "nvidia.com/gpu",
                                    "nominalQuota": "16",
                                    "borrowingLimit": "0",
                                }
                            ],
                        }
                        for pool in pools
                    ],
                }
            ],
        }
    }

    def checked_read(command, **kwargs):
        assert command[0] == "kubectl"
        assert "--request-timeout=30s" in command
        assert command[4:] == [
            "get",
            "clusterqueue",
            "inference-accelerators",
            "-n",
            "fs2-models",
            "-o",
            "json",
        ]
        return SimpleNamespace(stdout=json.dumps(baseline))

    output = tmp_path / "plan"
    monkeypatch.setattr(qualifier.subprocess, "run", checked_read)
    monkeypatch.setattr(
        "sys.argv",
        [
            "qualify_capacity.py",
            "--context",
            "test-only",
            "--output",
            str(output),
            "--image",
            "example.invalid/worker@sha256:" + "0" * 64,
            "--run",
            "fs2-admission-qa-unit",
            "--single-cell-shape",
            shape,
            "--single-cell-concurrency",
            str(concurrency),
        ],
    )
    previous_umask = os.umask(0o077)
    try:
        qualifier.main()
    finally:
        os.umask(previous_umask)
    objects = json.loads((output / "objects.json").read_text())["items"]
    queue = next(o for o in objects if o["kind"] == "ClusterQueue")
    assert "cohortName" not in queue["spec"]
    assert queue["spec"]["preemption"]["withinClusterQueue"] == "Never"
    jobs = json.loads((output / "jobs.json").read_text())["items"]
    assert len(jobs) == 8 + concurrency + 2
    single_cell = [
        j
        for j in jobs
        if j["metadata"]["labels"]["fs2.nebius.ai/qa-lane"] == "single-cell"
    ]
    assert len(single_cell) == concurrency + 1
    for job in single_cell:
        resources = job["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"] == resources["limits"]
        assert resources["requests"]["ephemeral-storage"] == "128Gi"
        assert resources["requests"]["memory"] == (
            "262400Mi" if shape == "atlas" else "131328Mi"
        )
    single_pool = "h100-reserved-8x" if shape == "atlas" else "h100-ondemand-1x"
    quotas = {
        f["name"]: f["resources"][0]
        for f in queue["spec"]["resourceGroups"][0]["flavors"]
    }
    assert quotas["inference-" + single_pool]["nominalQuota"] == str(concurrency)
    assert all("borrowingLimit" not in r for r in quotas.values())
