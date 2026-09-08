from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from fs2_serve_catalog.artifacts import load_artifact_manifest
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA, ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import load_catalog
from test_api_mcp import build_runtime
from test_model_deployment_publication import revision, status_view

from fs2_serve.admin import AdminReadService
from fs2_serve.deployment_runtimes import SET_SCHEMA, DeploymentRuntimeError, bind_deployment_runtimes
from fs2_serve.dynamic_routes import DynamicRouteError, bind_dynamic_publication
from fs2_serve.mcp_server import build_mcp_server
from fs2_serve.model_deployment_publication import assess_model_publication
from fs2_serve.native_catalog import augment_native_catalog
from fs2_serve.registry import OperationalModel, Registry
from fs2_serve.settings import Settings


@pytest.fixture
def inputs(tmp_path: Path):
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    bindings = ServingBindings(catalog.digest, MappingProxyType({}))
    gateway = bind_gateway_catalog(catalog, bindings)
    entries = {
        model: json.loads((CATALOG_ROOT / "deployment-runtimes" / f"{model}-portable-h100.json").read_text())
        for model in ("molmim", "genmol")
    }
    path = tmp_path / "deployment-runtimes.json"
    return catalog, bindings, gateway, entries, path


def project(inputs, entries=None):
    catalog, bindings, gateway, originals, path = inputs
    path.write_text(json.dumps({"schema": SET_SCHEMA, "models": originals if entries is None else entries}))
    return bind_deployment_runtimes(gateway, catalog, bindings, path, catalog_dir=CATALOG_ROOT)


def test_absent_selection_keeps_canonical_catalog_identical(inputs):
    catalog, bindings, gateway, _, _ = inputs
    assert bind_deployment_runtimes(gateway, catalog, bindings, None, catalog_dir=Path("/absent")) is gateway


def test_selected_runtime_set_supports_kubernetes_projected_configmap(inputs):
    catalog, bindings, gateway, entries, path = inputs
    projection = path.parent / "..2026_09_07_09_00_00"
    projection.mkdir()
    (projection / path.name).write_text(json.dumps({"schema": SET_SCHEMA, "models": entries}))
    (path.parent / "..data").symlink_to(projection.name, target_is_directory=True)
    path.symlink_to(Path("..data") / path.name)

    projected = bind_deployment_runtimes(gateway, catalog, bindings, path, catalog_dir=CATALOG_ROOT)
    assert projected.model("molmim").runtime_image_digest == entries["molmim"]["record"]["runtime"]["image"]["digest"]


def test_all_shipped_runtime_candidates_validate_without_granting_public_routes(inputs):
    catalog, bindings, gateway, originals, path = inputs
    catalog = augment_native_catalog(catalog, CATALOG_ROOT, repo_root=REPO_ROOT)
    gateway = bind_gateway_catalog(catalog, bindings)
    native_inputs = catalog, bindings, gateway, originals, path
    entries = {}
    for path in (CATALOG_ROOT / "deployment-runtimes").glob("*.json"):
        entry = json.loads(path.read_text())
        entries[entry["model_id"]] = entry
    projected = project(native_inputs, entries)
    for model_id, entry in entries.items():
        model = projected.model(model_id)
        assert model.runtime_image_digest == entry["record"]["runtime"]["image"]["digest"]
        assert not model.routable
        assert not model.mcp_invocable


def test_cpu_reference_database_runtime_is_exact_and_reserves_no_gpu(inputs):
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes/msa-search-pdb70-portable-cpu.json").read_text())
    projected = project(inputs, {entry["model_id"]: entry})
    model = projected.model("msa-search-pdb70")
    manifest = load_artifact_manifest(
        CATALOG_ROOT
        / "deployment-runtimes/artifacts"
        / ("msa-search-pdb70-2a3cb71cb615b8534b3134013e9cbecf003339bc6f034c4e6545dfdf91229c52.json")
    )

    assert model.gpu_class == "CPU"
    assert model.gpu_allocation_count == 0
    assert not model.routable and not model.mcp_invocable
    assert manifest.kind == "reference-database"
    assert manifest.digest == entry["record"]["cache"]["artifact"]["manifest_digest"]
    assert manifest.expanded_bytes == entry["record"]["cache"]["artifact"]["expanded_bytes"]
    assert len(manifest.files) == 9


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("resources", "gpu", "class"), "cpu"),
        (("resources", "gpu", "count"), 1),
        (("resources", "gpu", "topology"), "single-gpu"),
        (("resources", "gpu", "b300_state"), "unverified"),
        (("cache", "owner"), "fs2-serve-localizer"),
        (("cache", "artifact", "kind"), "nim-cache"),
    ],
)
def test_cpu_runtime_resource_and_database_identity_fail_closed(inputs, path, value):
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes/msa-search-pdb70-portable-cpu.json").read_text())
    target = entry["record"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DeploymentRuntimeError, match="resource-matched artifact"):
        project(inputs, {entry["model_id"]: entry})


def test_gpu_runtime_cannot_claim_embedded_reference_database_contract(inputs):
    entries = copy.deepcopy(inputs[3])
    entry = entries["molmim"]
    entry["record"]["cache"]["owner"] = "runtime-image"
    entry["record"]["cache"]["artifact"]["kind"] = "reference-database"
    with pytest.raises(DeploymentRuntimeError, match="resource-matched artifact"):
        project(inputs, entries)


def test_retained_service_suffix_is_not_hardware_or_model_alias(inputs):
    entries = copy.deepcopy(inputs[3])
    entries["molmim"]["qualification"]["active_runtime"]["service"]["name"] = "molmim-b300"
    assert project(inputs, entries).model("molmim").gpu_class == "NVIDIA-H100-SXM5-80GB"
    entries["molmim"]["qualification"]["active_runtime"]["service"]["name"] = "genmol-b300"
    with pytest.raises(DeploymentRuntimeError, match="aliases another model"):
        project(inputs, entries)


def test_admin_and_wrapper_share_selected_acquisition_provenance_and_semantics(inputs):
    from fs2_serve.configuration import catalog_configuration_contracts
    from fs2_serve.deployment_runtimes import deployment_runtime_configuration_identity

    catalog, _, _, entries, _ = inputs
    canonical = catalog_configuration_contracts(catalog)
    selected = catalog_configuration_contracts(catalog, deployment_runtime_entries=entries)
    for model, entry in entries.items():
        fields = deployment_runtime_configuration_identity(entry)
        for key in (
            "acquisition_contract_sha256",
            "provenance_sha256",
            "semantic_health_contract_sha256",
            "runtime_image_digest",
            "model_revision",
            "artifact_manifest_sha256",
        ):
            assert getattr(selected[model], key) == fields[key]
        assert selected[model].acquisition_contract_sha256 != canonical[model].acquisition_contract_sha256
        assert selected[model].provenance_sha256 != canonical[model].provenance_sha256
        assert "nvidia-h100-sxm5-80gb" in selected[model].supported_accelerator_classes


def test_exact_selected_runtimes_preserve_archival_nim_and_never_grant_routes(inputs):
    catalog, _, gateway, entries, _ = inputs
    old_digest = catalog.digest
    projected = project(inputs)
    for model in entries:
        effective = projected.model(model)
        assert catalog.model(model).to_dict()["runtime"]["kind"] == "nim"
        assert gateway.model(model).runtime_kind == "nim"
        assert effective.runtime_kind == "custom"
        assert effective.binding is None and not effective.routable and not effective.mcp_invocable
        assert effective.qualification["runtime_origin"]["kind"] == "independent-runtime"
        assert effective.qualification["states"]["semantic_qualified"]
        assert not effective.qualification["states"]["http_mcp_qualified"]
        assert effective.qualification["evidence"]["http_mcp_acceptance_sha256"] is None
        assert effective.runtime_image_digest == entries[model]["record"]["runtime"]["image"]["digest"]
        assert effective.license_id == entries[model]["record"]["model"]["source"]["license"]["id"]
    assert catalog.digest == projected.catalog_digest == old_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "variant-alias",
        "source-revision",
        "image",
        "artifact",
        "route-authority",
        "unproven-public",
        "qualification-image",
    ],
)
def test_selected_identity_and_qualification_drift_fail_closed(inputs, mutation):
    entries = copy.deepcopy(inputs[3])
    entry = entries["molmim"]
    if mutation == "variant-alias":
        entry["variant_id"] = entries["genmol"]["variant_id"]
    elif mutation == "source-revision":
        entry["record"]["model"]["source"]["revision"] = "0" * 40
    elif mutation == "image":
        entry["record"]["runtime"]["image"]["reference"] = "registry/runtime:mutable"
    elif mutation == "artifact":
        entry["record"]["cache"]["artifact"]["manifest_digest"] = None
    elif mutation == "route-authority":
        entry["record"]["support"]["route_exposed"] = True
    elif mutation == "unproven-public":
        entry["qualification"]["states"]["http_mcp_qualified"] = True
    else:
        entry["qualification"]["active_runtime"]["runtime_image_digest"] = (
            "sha256:" + hashlib.sha256(b"wrong").hexdigest()
        )
    with pytest.raises(DeploymentRuntimeError):
        project(inputs, entries)


def test_selected_overlay_refuses_routable_static_binding(inputs):
    catalog, bindings, gateway, entries, path = inputs
    models = dict(gateway.models)
    models["molmim"] = replace(models["molmim"], routable=True)
    with pytest.raises(DeploymentRuntimeError, match="already-routable"):
        project((catalog, bindings, replace(gateway, models=MappingProxyType(models)), entries, path))


def test_dynamic_route_pins_image_and_artifact_even_without_archival_binding(inputs, cipher, hasher):
    projected_catalog = project(inputs)
    effective = projected_catalog.model("molmim")
    entry = inputs[3]["molmim"]
    item = revision()
    publication = assess_model_publication(item, status_view(item)).publication
    assert publication is not None
    publication = publication.model_copy(
        update={
            "model_ref": "molmim",
            "open_ai": False,
            "artifact_revision": effective.model_revision,
            "runtime_image": entry["record"]["runtime"]["image"]["reference"],
            "artifact_manifest_digest": "sha256:" + entry["record"]["cache"]["artifact"]["manifest_digest"],
            "mcp_tool_name": "molmim",
        }
    )
    until = datetime.now(UTC) + timedelta(minutes=5)
    routed = bind_dynamic_publication(effective, publication, valid_until=until)
    assert routed.routable
    catalog_models = dict(projected_catalog.models)
    catalog_models["molmim"] = routed
    runtime_registry = Registry(
        replace(projected_catalog, models=MappingProxyType(catalog_models)),
        {
            "molmim": OperationalModel(
                gateway=routed,
                max_attempts=2,
                max_gpu_seconds_per_attempt=10,
                retry_base_seconds=1,
            )
        },
    )
    tools = asyncio.run(build_mcp_server(build_runtime(runtime_registry, cipher, hasher)).list_tools())
    tool = next(item for item in tools if item.name == "molmim_native")
    assert tool.meta is not None
    assert tool.meta["fs2_qualification"] == {
        "kind": "selected-deployment-runtime",
        "authority": "explicit-deployment-runtime-record",
        "observed_at": None,
        "states": entry["qualification"]["states"],
    }
    admin_identity = AdminReadService._identity(runtime_registry.get("molmim"))
    assert admin_identity.qualification is not None
    assert admin_identity.qualification.model_dump(mode="json") == {
        "kind": "selected-deployment-runtime",
        "authority": "explicit-deployment-runtime-record",
        "observed_at": None,
        "states": entry["qualification"]["states"],
    }
    for field, value in (
        ("runtime_image", "registry/runtime@sha256:" + hashlib.sha256(b"wrong-image").hexdigest()),
        ("artifact_manifest_digest", "sha256:" + hashlib.sha256(b"wrong-artifact").hexdigest()),
    ):
        with pytest.raises(DynamicRouteError, match="selected deployment runtime"):
            bind_dynamic_publication(effective, publication.model_copy(update={field: value}), valid_until=until)


def test_settings_and_registry_explicit_selection_refresh(inputs, monkeypatch):
    catalog, _, _, _, path = inputs
    project(inputs)
    monkeypatch.setenv("FS2_DEPLOYMENT_RUNTIME_RECORDS_FILE", str(path))
    assert Settings().deployment_runtime_records_file == path
    bindings_file = path.with_name("bindings.json")
    bindings_file.write_text(
        json.dumps({"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": catalog.digest, "bindings": {}})
    )
    registry = Registry.load(
        CATALOG_ROOT,
        bindings_file,
        repo_root=REPO_ROOT,
        evidence_root=None,
        deployment_runtime_records_file=path,
        max_attempts=2,
        max_gpu_seconds_per_attempt=10,
        retry_base_seconds=1,
    )
    assert registry.get("molmim", require_enabled=False).gateway.runtime_kind == "custom"
    assert registry.revalidate()
    path.write_text(json.dumps({"schema": SET_SCHEMA, "models": {}}))
    assert registry.revalidate()
    assert registry.get("molmim", require_enabled=False).gateway.runtime_kind == "nim"


def test_registry_binds_terraform_cpu_route_after_the_selected_runtime(tmp_path: Path):
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes/msa-search-pdb70-portable-cpu.json").read_text())
    runtime_set = tmp_path / "deployment-runtimes.json"
    runtime_set.write_text(json.dumps({"schema": SET_SCHEMA, "models": {entry["model_id"]: entry}}))
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps({"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": catalog.digest, "bindings": {}})
    )
    value = entry["record"]
    routes = tmp_path / "lean-routes.json"
    routes.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/lean-routes/v4",
                "routes": [
                    {
                        "model_id": entry["model_id"],
                        "variant_id": entry["variant_id"],
                        "model_revision": value["model"]["source"]["revision"],
                        "runtime_image_digest": value["runtime"]["image"]["digest"],
                        "service": entry["qualification"]["active_runtime"]["service"],
                        "storage_mode": "ephemeral-emptydir",
                        "protocols": value["interface"]["endpoints"],
                        "operations": value["interface"]["policy"]["operations"],
                        "mcp": {
                            "enabled": True,
                            "tool_name": "msa_search",
                            "description": "Run an authorized PDB70 multiple-sequence-alignment search.",
                        },
                        "placement": {
                            "region": "eu-north1",
                            "accelerator_class": "CPU",
                            "pool_id": "general-cpu-8x",
                        },
                    }
                ],
            }
        )
    )

    registry = Registry.load(
        CATALOG_ROOT,
        bindings,
        repo_root=REPO_ROOT,
        evidence_root=None,
        lean_routes_file=routes,
        deployment_runtime_records_file=runtime_set,
        max_attempts=2,
        max_gpu_seconds_per_attempt=10,
        retry_base_seconds=1,
    )
    model = registry.get("msa-search-pdb70")

    assert model.lean_static
    assert model.gateway.runtime_kind == "custom"
    assert model.gateway.model_revision == value["model"]["source"]["revision"]
    assert model.gateway.runtime_image_digest == value["runtime"]["image"]["digest"]
    assert model.gateway.gpu_class == "CPU"
    assert model.gateway.gpu_allocation_count == 0
    assert dict(model.gateway.endpoints) == value["interface"]["endpoints"]
    assert model.binding.backend_gpu_class == "CPU"
    assert model.binding.artifact_manifest_digest == value["cache"]["artifact"]["manifest_digest"]
    assert model.binding.service_origin == "http://msa-search-pdb70.fs2-models.svc.cluster.local:8000"
