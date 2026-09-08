"""Bootstrap CPU scaling preserves existing static CPU and GPU contracts."""

from __future__ import annotations

import json

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from fs2_serve_catalog.loader import load_catalog
from pydantic import ValidationError
from test_admin_configuration import cpu_runtime_configuration, qualified_configuration

from fs2_serve.configuration import (
    ConfigurationService,
    InMemoryConfigurationRepository,
    StaticCatalogConfigurationAdapter,
    catalog_configuration_contracts,
    configuration_etag,
)
from fs2_serve.configuration_models import (
    AcceleratorPoolConfiguration,
    PlacementConfiguration,
    PlatformConfiguration,
)
from fs2_serve.native_catalog import augment_native_catalog

MIB = 1024**2


def managed_cpu_payload():
    legacy, _ = cpu_runtime_configuration()
    payload = legacy.model_dump(mode="json")
    payload["pools"]["batch-cpu"].update(allocatable_cpu_millis=7000, allocatable_memory_bytes=28672 * MIB)
    model = payload["models"].pop("msa-search-pdb70")
    model["model_id"] = "phenoage"
    model["placement"].update(cpu_millis=1000, memory_bytes=256 * MIB)
    model["autoscaling"].update(min_replicas=0, max_replicas=14)
    payload["models"]["phenoage"] = model
    return payload


def test_new_optional_fields_preserve_exact_gpu_and_static_msa_identities():
    gpu, _ = qualified_configuration()
    msa, _ = cpu_runtime_configuration()
    assert configuration_etag(gpu) == "76aa456c6535b7e1fb3e2bf088957be82dc4bbe110f27574ff27535e4c54df5f"
    assert configuration_etag(msa) == "23dce3c7d049252fcdbcdaa2e72c2bc65733353d77d28e18261b5e048f37971b"
    for original in (gpu, msa):
        payload = original.model_dump(mode="json")
        for pool in payload["pools"].values():
            assert "allocatable_cpu_millis" not in pool and "allocatable_memory_bytes" not in pool
        for model in payload["models"].values():
            assert "cpu_millis" not in model["placement"] and "memory_bytes" not in model["placement"]
        assert configuration_etag(PlatformConfiguration.model_validate(payload)) == configuration_etag(original)


def test_managed_cpu_allows_explicit_zero_to_n_from_bootstrap():
    value = PlatformConfiguration.model_validate(managed_cpu_payload())
    model = value.models["phenoage"]
    assert (model.autoscaling.min_replicas, model.autoscaling.max_replicas) == (0, 14)
    assert model.placement.accelerators == 0
    assert value.pools["batch-cpu"].accelerators_per_node == 0
    assert value.pools["batch-cpu"].resource_name == "cpu"
    assert model.placement.cpu_millis == 1000 and model.placement.memory_bytes == 256 * MIB


def test_legacy_static_msa_does_not_inherit_native_cpu_scaling():
    legacy, _ = cpu_runtime_configuration()
    payload = legacy.model_dump(mode="json")
    payload["pools"]["batch-cpu"].update(allocatable_cpu_millis=7000, allocatable_memory_bytes=28672 * MIB)
    assert PlatformConfiguration.model_validate(payload).models["msa-search-pdb70"].autoscaling.min_replicas == 1
    payload["models"]["msa-search-pdb70"]["autoscaling"]["min_replicas"] = 0
    with pytest.raises(ValidationError, match="one static replica"):
        PlatformConfiguration.model_validate(payload)


@pytest.mark.parametrize("changes,maximum", [({"cpu_millis": 2000}, 6), ({"memory_bytes": 16384 * MIB}, 2)])
def test_managed_cpu_ceiling_uses_limiting_resource_on_each_node(changes, maximum):
    payload = managed_cpu_payload()
    payload["models"]["phenoage"]["placement"].update(changes)
    payload["models"]["phenoage"]["autoscaling"]["max_replicas"] = maximum
    PlatformConfiguration.model_validate(payload)
    payload["models"]["phenoage"]["autoscaling"]["max_replicas"] = maximum + 1
    with pytest.raises(ValidationError, match="ceiling exceeds"):
        PlatformConfiguration.model_validate(payload)


def test_managed_cpu_headroom_sums_distinct_declared_pools_not_accelerators():
    payload = managed_cpu_payload()
    payload["pools"]["second-cpu"] = {
        **payload["pools"]["batch-cpu"],
        "max_nodes": 1,
        "allocatable_cpu_millis": 2000,
    }
    model = payload["models"]["phenoage"]
    model["placement"]["pool_ids"].append("second-cpu")
    model["autoscaling"]["max_replicas"] = 16
    PlatformConfiguration.model_validate(payload)
    model["autoscaling"]["max_replicas"] = 17
    with pytest.raises(ValidationError, match="ceiling exceeds"):
        PlatformConfiguration.model_validate(payload)


@pytest.mark.parametrize("field,value", [("cpu_millis", 8000), ("memory_bytes", 32768 * MIB)])
def test_managed_cpu_cannot_fit_a_replica_by_summing_separate_nodes(field, value):
    payload = managed_cpu_payload()
    payload["models"]["phenoage"]["placement"][field] = value
    with pytest.raises(ValidationError, match="cannot fit"):
        PlatformConfiguration.model_validate(payload)


def test_managed_cpu_requires_real_declared_pool_capacity():
    payload = managed_cpu_payload()
    payload["pools"]["batch-cpu"].pop("allocatable_cpu_millis")
    payload["pools"]["batch-cpu"].pop("allocatable_memory_bytes")
    with pytest.raises(ValidationError, match="requires declared CPU and memory"):
        PlatformConfiguration.model_validate(payload)


@pytest.mark.parametrize("field", ["cpu_millis", "memory_bytes"])
def test_managed_cpu_placement_metadata_requires_positive_complete_pair(field):
    placement = managed_cpu_payload()["models"]["phenoage"]["placement"]
    placement.pop(field)
    with pytest.raises(ValidationError, match="both full-Pod"):
        PlacementConfiguration.model_validate(placement)
    placement[field] = 0
    with pytest.raises(ValidationError):
        PlacementConfiguration.model_validate(placement)


@pytest.mark.parametrize("field", ["allocatable_cpu_millis", "allocatable_memory_bytes"])
def test_managed_cpu_pool_metadata_requires_positive_complete_pair(field):
    pool = managed_cpu_payload()["pools"]["batch-cpu"]
    pool.pop(field)
    with pytest.raises(ValidationError, match="both allocatable"):
        AcceleratorPoolConfiguration.model_validate(pool)
    pool[field] = 0
    with pytest.raises(ValidationError):
        AcceleratorPoolConfiguration.model_validate(pool)


def test_managed_cpu_metadata_cannot_change_a_gpu_resource_contract():
    legacy_gpu, _ = qualified_configuration()
    gpu_pool = next(iter(legacy_gpu.pools.values())).model_dump()
    gpu_pool.update(allocatable_cpu_millis=7000, allocatable_memory_bytes=28672 * MIB)
    with pytest.raises(ValidationError, match="only for CPU"):
        AcceleratorPoolConfiguration.model_validate(gpu_pool)
    placement = managed_cpu_payload()["models"]["phenoage"]["placement"]
    placement["accelerators"] = 1
    with pytest.raises(ValidationError, match="zero accelerators"):
        PlacementConfiguration.model_validate(placement)


@pytest.mark.asyncio
async def test_actual_phenoage_bootstrap_contract_accepts_zero_with_exact_native_identity():
    catalog = augment_native_catalog(load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT), CATALOG_ROOT, repo_root=REPO_ROOT)
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes/phenoage-cpu.json").read_text())
    contracts = catalog_configuration_contracts(catalog, deployment_runtime_entries={"phenoage": entry})
    contract = contracts["phenoage"]
    payload = managed_cpu_payload()
    payload["models"]["phenoage"]["artifact"] = {
        "image_repository": entry["record"]["runtime"]["image"]["reference"].split("@", 1)[0],
        "image_digest": contract.runtime_image_digest,
        "model_revision": contract.model_revision,
        "artifact_manifest_sha256": contract.artifact_manifest_sha256,
        "acquisition_contract_sha256": contract.acquisition_contract_sha256,
        "provenance_sha256": contract.provenance_sha256,
        "semantic_health_contract_sha256": contract.semantic_health_contract_sha256,
    }
    configuration = PlatformConfiguration.model_validate(payload)
    validation = await ConfigurationService(
        repository=InMemoryConfigurationRepository(configuration),
        catalog=StaticCatalogConfigurationAdapter(contracts),
    ).validate_bootstrap(configuration)
    assert validation.valid and validation.issues == []
    assert not entry["qualification"]["states"]["elasticity_qualified"]
