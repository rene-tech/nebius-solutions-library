"""Only digest-verified frozen metadata may be reused across fresh state reads."""

import copy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from fastapi import HTTPException
from test_gromacs_compact_capability import active_workload

from fs2_serve.scientific_batch import codec
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.workload_routes import authorize_workload_capability


@pytest.fixture(scope="module")
def compact():
    state, _, _ = active_workload(20000)
    value = codec.state_to_value(state)
    assert value["input_manifest"]["encoding"] == codec.COMPACT_METADATA_SCHEMA
    return value


@pytest.fixture
def cache(monkeypatch):
    value = codec._ImmutableMetadataCache()
    monkeypatch.setattr(codec, "_IMMUTABLE_METADATA_CACHE", value)
    return value


def test_cache_reuses_frozen_graph_but_not_mutable_state(compact, cache, monkeypatch):
    original = codec.state_from_value(compact)
    assert len(cache.entries) == 1 and 0 < cache.retained_bytes <= cache.max_bytes
    changed = copy.deepcopy(compact)
    changed["cancel_requested"] = True
    changed["revision"] += 1
    changed["stages"][0]["attempts"][0]["deletion_requested"] = False

    def no_decode(_):
        raise AssertionError("verified immutable cache hit must not re-decode twenty thousand inputs")

    monkeypatch.setattr(codec, "_unpack_metadata", no_decode)
    current = codec.state_from_value(changed)
    assert current.cancel_requested and current.revision == original.revision + 1
    assert current is not original and current.stages is not original.stages
    assert current.input_manifest is original.input_manifest
    assert current.execution_plan is original.execution_plan
    with pytest.raises(FrozenInstanceError):
        current.input_manifest.entries[0].size_bytes = 1
    with pytest.raises(FrozenInstanceError):
        current.execution_plan.invocations[0].materializations[0].destination = "/wrong"


@pytest.mark.parametrize("damage", ["data", "sha256", "size", "extra", "plan", "tenant"])
def test_changed_or_corrupt_state_cannot_hide_behind_warm_cache(compact, cache, damage):
    codec.state_from_value(compact)
    changed = copy.deepcopy(compact)
    if damage == "data":
        changed["input_manifest"]["data"] = "not-valid-base64"
    elif damage == "sha256":
        changed["input_manifest"]["sha256"] = "0" * 64
    elif damage == "size":
        changed["input_manifest"]["size_bytes"] += 1
    elif damage == "extra":
        changed["adapter_execution"]["unexpected"] = True
    elif damage == "plan":
        changed["plan"]["stages"][0]["stage_id"] = "another-stage"
    else:
        changed["tenant_id"] = "someone-else"
    with pytest.raises(ValueError):
        codec.state_from_value(changed)
    assert len(cache.entries) == 1


def test_cache_evicts_by_retained_graph_bytes_and_count(compact):
    state = codec.state_from_value(compact)
    graph = (state.input_manifest, state.execution_plan)
    weight = codec._retained_metadata_bytes(("a", graph))
    cache = codec._ImmutableMetadataCache(max_bytes=weight * 2, max_entries=2)
    cache.put("a", graph)
    cache.put("b", graph)
    assert cache.get("a") is graph  # promote a; b becomes oldest
    cache.put("c", graph)
    assert cache.get("b") is None and cache.get("a") is graph and cache.get("c") is graph
    assert cache.retained_bytes <= cache.max_bytes and len(cache.entries) == 2
    small = codec._ImmutableMetadataCache(max_bytes=weight - 1, max_entries=2)
    small.put("a", graph)
    assert not small.entries and small.retained_bytes == 0
    bytes_only = codec._ImmutableMetadataCache(max_bytes=weight, max_entries=4)
    bytes_only.put("a", graph)
    bytes_only.put("b", graph)
    assert bytes_only.get("a") is None and bytes_only.get("b") is graph


def test_legacy_uncompressed_states_keep_exact_validation_and_do_not_fill_cache(cache):
    state, _, _ = active_workload(1)
    value = codec.state_to_value(state)
    assert codec.state_from_value(value) == state
    assert not cache.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["cancel", "attempt"])
async def test_warm_metadata_cache_keeps_current_attempt_and_cancellation_fences(hasher, cache, changed):
    state, resource, _ = active_workload(20000)
    raw = codec.state_to_value(state)

    class Repository:
        async def get(self, operation_id, *, tenant_id):
            assert (operation_id, tenant_id) == (state.operation_id, state.tenant_id)
            return codec.state_from_value(raw)

    authority = ScientificWorkloadCapabilityAuthority(hasher)
    token = "Bearer " + authority.issue(resource)
    await authorize_workload_capability(authority, Repository(), token)
    assert len(cache.entries) == 1
    if changed == "cancel":
        raw["cancel_requested"] = True
    else:
        raw["stages"][0]["attempts"][0]["attempt_id"] = str(uuid4())
    with pytest.raises(HTTPException) as denied:
        await authorize_workload_capability(authority, Repository(), token)
    assert denied.value.status_code == 409
