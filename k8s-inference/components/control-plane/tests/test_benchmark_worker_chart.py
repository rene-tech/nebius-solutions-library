import subprocess

import pytest
from test_helm_chart import TEST_DIGEST, render, render_command


def values():
    return [
        "--set",
        "benchmarkWorkers.enabled=true",
        "--set",
        f"benchmarkWorkers.image=registry.example.test/benchmarks@{TEST_DIGEST}",
        "--set",
        "benchmarkWorkers.sourceCommit=" + "a" * 40,
        "--set",
        "benchmarkWorkers.credentialSecret=benchmark-inference-key",
    ]


def test_workers_are_opt_in_and_do_not_add_a_gpu_scheduler():
    assert not any(d["metadata"]["name"].endswith("-benchmarks") for d in render())
    documents = render(*values())
    deployment = next(d for d in documents if d["metadata"]["name"].endswith("-benchmarks"))
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 4
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    worker = pod["containers"][0]
    assert worker["args"][0] == "pool"
    assert "nvidia.com/gpu" not in worker["resources"]["requests"]
    secrets = {v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v}
    assert secrets == {"benchmark-inference-key", "fs2-serve-admin"}


@pytest.mark.parametrize(
    "setting", ["replicas=0", "replicas=17", "image=mutable:latest", "sourceCommit=main", "credentialSecret="]
)
def test_workers_reject_invalid_release_values(setting):
    result = subprocess.run(  # noqa: S603 - fixed Helm test arguments
        render_command(*values(), "--set", "benchmarkWorkers." + setting), capture_output=True
    )
    assert result.returncode != 0
