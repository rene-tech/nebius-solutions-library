from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA, ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import load_catalog
from test_model_deployment_publication import revision, status_view

from fs2_serve.deployment_runtimes import SET_SCHEMA, DeploymentRuntimeError, bind_deployment_runtimes
from fs2_serve.dynamic_routes import DynamicRouteError, bind_dynamic_publication
from fs2_serve.model_deployment_publication import assess_model_publication
from fs2_serve.registry import Registry
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


def test_dynamic_route_pins_image_and_artifact_even_without_archival_binding(inputs):
    effective = project(inputs).model("molmim")
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
        }
    )
    until = datetime.now(UTC) + timedelta(minutes=5)
    assert bind_dynamic_publication(effective, publication, valid_until=until).routable
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
