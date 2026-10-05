"""Actual large-manifest validation must leave the API event loop responsive."""

import asyncio
import threading
import time

import pytest
from test_scientific_artifact_reads import (
    test_large_input_manifest_reads128_rows_at_a_time_and_verifies_each_pointer as large_input,
)

from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog


@pytest.mark.asyncio
async def test_twenty_thousand_entry_validation_runs_off_loop_with_heartbeat(monkeypatch):
    original = ScientificProfileCatalog.validate_artifact_manifest
    owner = threading.get_ident()
    entered = threading.Event()
    finished = False
    ticks = []

    def measured(self, value):
        assert threading.get_ident() != owner
        assert len(value["entries"]) == 20000
        entered.set()
        return original(self, value)

    async def heartbeat():
        while not finished:
            if entered.is_set():
                ticks.append(time.monotonic())
            await asyncio.sleep(0.002)

    monkeypatch.setattr(ScientificProfileCatalog, "validate_artifact_manifest", measured)
    peer = asyncio.create_task(heartbeat())
    try:
        await large_input(None)
    finally:
        finished = True
        await peer
    assert len(ticks) >= 10
    assert max(right - left for left, right in zip(ticks, ticks[1:], strict=False)) < 1
