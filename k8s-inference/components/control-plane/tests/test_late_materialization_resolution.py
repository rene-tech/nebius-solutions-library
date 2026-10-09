"""Large retained input maps must not quadratically stall shared API readers."""

import asyncio
from dataclasses import replace

import pytest
from test_gromacs_compact_capability import active_workload

from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.models import VerifiedInputManifest


@pytest.mark.asyncio
async def test_twenty_thousand_inputs_resolve_once_in_order_without_starving_peers(monkeypatch):
    state, resource, _ = active_workload(20000)

    def prohibit_repeated_linear_lookup(*args):
        raise AssertionError("large resolution must index immutable inputs once")

    monkeypatch.setattr(VerifiedInputManifest, "artifact", prohibit_repeated_linear_lookup)
    ticks = 0
    finished = False

    async def heartbeat():
        nonlocal ticks
        while not finished:
            ticks += 1
            await asyncio.sleep(0)

    peer = asyncio.create_task(heartbeat())
    try:
        resolved = await ScientificBatchController._resolve_materializations(object(), None, state, resource.invocation)
    finally:
        finished = True
        await peer
    assert ticks >= 20000 // 128
    assert len(resolved) == 20000
    assert [item.artifact_id for item in resolved] == [item.artifact_id for item in state.input_manifest.entries]
    assert [item.digest for item in resolved] == [item.digest for item in state.input_manifest.entries]
    assert [item.destination for item in resolved] == [
        item.destination for item in resource.invocation.materializations
    ]


@pytest.mark.asyncio
async def test_missing_input_still_requires_a_fenced_predecessor():
    state, resource, _ = active_workload(1)
    materialization = replace(resource.invocation.materializations[0], artifact_id="unknown-input")
    invocation = replace(resource.invocation, consumes=("unknown-input",), materializations=(materialization,))
    with pytest.raises(RuntimeError, match="logical artifact has no producer"):
        await ScientificBatchController._resolve_materializations(object(), None, state, invocation)
