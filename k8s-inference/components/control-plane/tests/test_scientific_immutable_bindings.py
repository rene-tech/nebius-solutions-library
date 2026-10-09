"""A metadata memo must never memoize an attempt's authority or live state."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_gromacs_compact_capability import active_workload

from fs2_serve.scientific_batch import capability as module
from fs2_serve.scientific_batch.codec import _retained_metadata_bytes
from fs2_serve.scientific_batch.workload_routes import authorize_workload_capability


@pytest.fixture
def cache(monkeypatch):
    value = module._ImmutableBindingsCache()
    monkeypatch.setattr(module, "_IMMUTABLE_BINDINGS", value)
    return value


@pytest.mark.asyncio
async def test_twenty_thousand_bindings_reuse_only_frozen_metadata(cache, hasher, monkeypatch):
    state, resource, repository = active_workload(20000)
    authority = module.ScientificWorkloadCapabilityAuthority(hasher)
    token = "Bearer " + authority.issue(resource)
    first, _, _ = await authorize_workload_capability(authority, repository, token)
    assert len(first.artifacts) == 20000 and len(cache.entries) == 1
    assert 0 < cache.retained_bytes <= cache.max_bytes

    def prohibit_rebuilding(*args):
        raise AssertionError("warm immutable inputs must not be rebuilt on every batch")

    monkeypatch.setattr(module, "_build_input_bindings", prohibit_rebuilding)
    peers = await asyncio.gather(*(authorize_workload_capability(authority, repository, token) for _ in range(12)))
    assert all(value.artifacts is first.artifacts and value is not first for value, _, _ in peers)
    assert all(current is state for _, current, _ in peers)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manifest", "invocation", "tenant", "attempt", "cancel", "access", "digest"])
async def test_warm_metadata_never_hides_changed_inputs_or_current_fences(cache, hasher, change):
    state, resource, repository = active_workload(257)
    authority = module.ScientificWorkloadCapabilityAuthority(hasher)
    token = "Bearer " + authority.issue(resource)
    await authorize_workload_capability(authority, repository, token)
    assert len(cache.entries) == 1
    if change == "manifest":
        entries = state.input_manifest.entries
        state = replace(
            state,
            input_manifest=replace(state.input_manifest, entries=(replace(entries[0], size_bytes=9), *entries[1:])),
        )
    elif change == "invocation":
        invocation = state.execution_plan.invocations[0]
        state = replace(
            state,
            execution_plan=replace(
                state.execution_plan,
                invocations=(replace(invocation, materializations=tuple(reversed(invocation.materializations))),),
            ),
        )
    elif change == "tenant":
        state = replace(state, tenant_id="foreign", access_context=replace(state.access_context, tenant_id="foreign"))
    elif change == "attempt":
        stage = state.stages[0]
        state = replace(state, stages=(replace(stage, attempts=(replace(stage.attempts[0], attempt_id=uuid4()),)),))
    elif change == "cancel":
        state = replace(state, cancel_requested=True)
    elif change == "access":
        state = replace(
            state, access_context=replace(state.access_context, profile="academic", receipt_digest="sha256:" + "b" * 64)
        )
    else:
        # This is a correctly signed claim for a DIFFERENT immutable input
        # identity, not a fake verifier or an unauthenticated token.
        changed = SimpleNamespace(**vars(resource))
        changed.materializations = (replace(resource.materializations[0], size_bytes=9), *resource.materializations[1:])
        token = "Bearer " + authority.issue(changed)
    repository.records[state.operation_id] = state
    with pytest.raises(HTTPException) as denied:
        await authorize_workload_capability(authority, repository, token)
    assert denied.value.status_code in {401, 409}


@pytest.mark.asyncio
async def test_strong_source_identity_and_byte_count_lru_bounds(cache):
    state, _, _ = active_workload(257)
    manifest, invocation = state.input_manifest, state.execution_plan.invocations[0]
    bindings = await module.immutable_input_bindings(manifest, invocation)
    other, third = replace(invocation), replace(invocation)
    assert cache.get(manifest, other) is None
    retained = (manifest, invocation, bindings)
    weight = _retained_metadata_bytes(((id(manifest), id(invocation)), retained))
    count = module._ImmutableBindingsCache(max_bytes=weight * 4, max_entries=2)
    count.put(manifest, invocation, bindings)
    count.put(manifest, other, bindings)
    assert count.get(manifest, invocation) is bindings
    count.put(manifest, third, bindings)
    assert count.get(manifest, other) is None and count.get(manifest, invocation) is bindings
    assert len(count.entries) == 2 and count.retained_bytes <= count.max_bytes
    for value, _ in count.entries.values():
        assert value[0] is manifest
        assert value[1] is invocation or value[1] is third
    tiny = module._ImmutableBindingsCache(max_bytes=weight - 1)
    tiny.put(manifest, invocation, bindings)
    assert not tiny.entries and tiny.retained_bytes == 0
    bytes_only = module._ImmutableBindingsCache(max_bytes=weight + 256, max_entries=4)
    bytes_only.put(manifest, invocation, bindings)
    bytes_only.put(manifest, other, bindings)
    assert bytes_only.get(manifest, invocation) is None
    assert bytes_only.get(manifest, other) is bindings and len(bytes_only.entries) == 1


@pytest.mark.asyncio
async def test_concurrent_cold_resolutions_keep_identical_digest_and_accounting(cache):
    state, _, _ = active_workload(1024)
    manifest, invocation = state.input_manifest, state.execution_plan.invocations[0]
    values = await asyncio.gather(*(module.immutable_input_bindings(manifest, invocation) for _ in range(10)))
    assert all(value == values[0] for value in values)
    assert len(cache.entries) == 1
    assert cache.retained_bytes == sum(weight for _, weight in cache.entries.values())
