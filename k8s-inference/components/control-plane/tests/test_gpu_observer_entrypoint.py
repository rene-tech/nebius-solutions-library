from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fs2_serve import gpu_allocation_observer_cli as cli
from fs2_serve.gpu_identity import pod_gpu_count
from fs2_serve.settings import Settings

CONTROL_ROOT = Path(__file__).resolve().parents[1]


def test_observer_dispatch_does_not_import_gateway_or_controller() -> None:
    source = """
import sys
from fs2_serve.entrypoint import main
sys.argv = ['fs2-serve', 'gpu-allocation-observer', '--help']
try:
    main()
except SystemExit as error:
    assert error.code == 0
assert not {'fs2_serve.cli', 'fs2_serve.api', 'fs2_serve.mcp_server',
            'fs2_serve.runtime_kubernetes', 'fs2_serve.model_deployment',
            'fs2_serve.model_deployment_controller', 'uvicorn', 'fastapi'} & sys.modules.keys()
"""
    subprocess.run(  # noqa: S603 - fixed interpreter and test-owned source
        [sys.executable, "-c", source],
        env={**os.environ, "PYTHONPATH": str(CONTROL_ROOT / "src")},
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.asyncio
async def test_observer_uses_same_settings_and_plural_namespace_contract(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(cli, "run_gpu_allocation_observer", run)
    settings = Settings(
        gpu_allocation_observer_node_name="node-1",
        gpu_allocation_observer_namespace="legacy",
        gpu_allocation_observer_namespaces=("fs2-models", "fs2-academic-poc"),
        gpu_allocation_observer_poll_seconds=1,
    )
    await cli.observe_gpu_allocations(settings)
    run.assert_awaited_once()
    assert run.call_args.kwargs["checkpoint_file"] == settings.gpu_allocation_observer_checkpoint_file
    publisher = run.call_args.kwargs["publisher"]
    assert publisher.namespaces == ("fs2-models", "fs2-academic-poc")
    assert publisher.node_name == "node-1"
    assert publisher.poll_seconds == 1
    assert publisher.token_file == settings.gpu_allocation_observer_token_file
    assert publisher.ca_file == settings.gpu_allocation_observer_ca_file


@pytest.mark.asyncio
async def test_observer_missing_node_still_fails_before_network(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(cli, "run_gpu_allocation_observer", run)
    with pytest.raises(RuntimeError, match="requires its Kubernetes node name"):
        await cli.observe_gpu_allocations(Settings(gpu_allocation_observer_node_name=None))
    run.assert_not_awaited()


@pytest.mark.parametrize("resource", ["nvidia.com/gpu", "nvidia.com/mig-1g.10gb", "amd.com/gpu", "gpu.intel.com/xe"])
@pytest.mark.parametrize("requested,limit,expected", [(1, 1, 1), ("2", "2", 2), (1, 2, None), (True, 1, None)])
def test_shared_allocation_count_preserves_exact_resource_identity(resource, requested, limit, expected) -> None:
    pod = {"spec": {"containers": [{"resources": {"requests": {resource: requested}, "limits": {resource: limit}}}]}}
    assert pod_gpu_count(pod) == expected


def test_runtime_and_observer_reuse_one_count_implementation() -> None:
    from fs2_serve import gpu_allocation_observer, runtime_kubernetes

    assert gpu_allocation_observer.pod_gpu_count is pod_gpu_count
    assert runtime_kubernetes.pod_gpu_count is pod_gpu_count
