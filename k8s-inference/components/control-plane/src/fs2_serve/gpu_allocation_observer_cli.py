"""Run node-local allocation observation without importing the gateway stack."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .gpu_allocation_observer import KubernetesGpuAllocationPublisher, run_gpu_allocation_observer
from .settings import Settings


async def observe_gpu_allocations(settings: Settings) -> None:
    if settings.gpu_allocation_observer_node_name is None:
        raise RuntimeError("GPU allocation observer requires its Kubernetes node name")
    await run_gpu_allocation_observer(
        publisher=KubernetesGpuAllocationPublisher(
            base_url=settings.gpu_allocation_observer_api_url,
            token_file=settings.gpu_allocation_observer_token_file,
            ca_file=settings.gpu_allocation_observer_ca_file,
            namespaces=settings.gpu_allocation_observer_namespace_set(),
            node_name=settings.gpu_allocation_observer_node_name,
            poll_seconds=settings.gpu_allocation_observer_poll_seconds,
        ),
        checkpoint_file=settings.gpu_allocation_observer_checkpoint_file,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="fs2-serve")
    parser.add_argument("command", choices=("gpu-allocation-observer",))
    parser.parse_args()
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(observe_gpu_allocations(settings))


if __name__ == "__main__":
    main()
