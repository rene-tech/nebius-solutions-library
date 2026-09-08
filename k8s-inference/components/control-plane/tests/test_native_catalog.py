from __future__ import annotations

import copy
import hashlib
import json
import shutil
from types import MappingProxyType

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA, ServingBindings, bind_gateway_catalog
from fs2_serve_catalog.loader import CatalogError, _canonical_bytes, load_catalog

from fs2_serve.configuration import catalog_configuration_contracts
from fs2_serve.deployment_runtimes import SET_SCHEMA, DeploymentRuntimeError, bind_deployment_runtimes
from fs2_serve.native_catalog import augment_native_catalog
from fs2_serve.registry import Registry, RegistryError


@pytest.fixture
def archive():
    return load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)


@pytest.fixture
def native_tree(tmp_path):
    path = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, path)
    return path


def write_declaration(root, value):
    (root / "native/phenoage.json").write_text(json.dumps(value))


def declaration(root):
    return json.loads((root / "native/phenoage.json").read_text())


def selected_entries():
    return {
        model: json.loads((CATALOG_ROOT / "deployment-runtimes" / filename).read_text())
        for model, filename in (("phenoage", "phenoage-cpu.json"), ("altumage", "altumage-cuda.json"))
    }


def registry(root, tmp_path, archive, *, entries=None, binding_digest=None):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "schema": SERVING_BINDINGS_SCHEMA,
                "catalog_digest": binding_digest or archive.digest,
                "bindings": {},
            }
        )
    )
    selections = None
    if entries is not None:
        selections = tmp_path / "selected.json"
        selections.write_text(json.dumps({"schema": SET_SCHEMA, "models": entries}))
    return Registry.load(
        root,
        bindings,
        repo_root=REPO_ROOT,
        evidence_root=None,
        deployment_runtime_records_file=selections,
        max_attempts=2,
        max_gpu_seconds_per_attempt=60,
        retry_base_seconds=0.01,
    )


def test_no_native_directory_preserves_exact_object(archive, tmp_path):
    assert augment_native_catalog(archive, tmp_path) is archive


def test_native_semantics_validate_with_only_installed_repository_mirror(archive, native_tree, tmp_path):
    (native_tree / "packaged-repository").rename(native_tree / "repository")
    augmented = augment_native_catalog(archive, native_tree, repo_root=tmp_path / "absent-checkout")
    assert augmented.semantic_request_contract("phenoage").state == "qualified"
    assert augmented.semantic_request_contract("altumage").state == "qualified"


def test_native_records_do_not_rewrite_archival_digests_or_qualification(archive):
    augmented = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    assert len(archive.records) == 16
    assert set(augmented.records) == set(archive.records) | {"phenoage", "altumage"}
    assert augmented.digest == archive.digest
    assert augmented.tested_model_ids == archive.tested_model_ids
    assert augmented.blocked_candidate_ids == archive.blocked_candidate_ids
    for field in (
        "records",
        "model_variants",
        "fallback_candidates",
        "semantic_requests",
        "scale_contracts",
        "acquisition_plans",
        "compatibility_audit",
    ):
        for identity, item in getattr(archive, field).items():
            assert getattr(augmented, field)[identity] is item
    old = catalog_configuration_contracts(archive)
    new = catalog_configuration_contracts(augmented)
    for model_id in archive.records:
        assert old[model_id] == new[model_id]


def test_native_graph_is_exact_source_cpu_formula_or_cuda_weights_without_routes(archive):
    augmented = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    gateway = bind_gateway_catalog(augmented, ServingBindings(archive.digest, MappingProxyType({})))
    for model_id, architecture, artifact_kind in (("phenoage", "cpu", "formula"), ("altumage", "cuda", "weights")):
        value = augmented.model(model_id).to_dict()
        (variant,) = augmented.variants_for(model_id)
        fallback, profile = augmented.fallback_for_variant(variant.variant_id)
        assert variant.base_model_id == variant.exposed_model_id == fallback.lane_id == model_id
        assert profile == architecture == variant.runtime_architecture
        assert variant.to_dict()["source"] == value["model"]["source"]
        assert variant.to_dict()["relationship"]["nim_artifact_parity"] == "not-applicable"
        plan = augmented.acquisition_plan(model_id)
        assert plan.method == "runtime-image"
        assert plan.to_dict()["artifact_kind"] == artifact_kind
        assert plan.to_dict()["runtime_image"] == value["runtime"]["image"]
        scale = augmented.scale_contract(model_id)
        assert scale.activation_mode == "replica-scale"
        assert scale.to_dict()["readiness"] == value["interface"]["readiness"]
        assert scale.to_dict()["target"]["name"] == model_id
        semantic = augmented.semantic_request_contract(model_id)
        assert semantic.to_dict()["serialization"] == "sha256-json-compact-no-newline/v1"
        assert len(set(semantic.request_sha256)) == 2
        model = gateway.model(model_id)
        assert not model.routable and not model.mcp_invocable and model.binding is None
        assert model.qualification is None
    assert gateway.model("phenoage").gpu_allocation_count == 0
    assert gateway.model("altumage").gpu_allocation_count == 1
    assert augmented.model("altumage").to_dict()["resources"]["gpu"]["b300_state"] == "unverified"


def test_registry_selected_native_records_share_bootstrap_identity_but_do_not_grant_routes(archive, tmp_path):
    entries = selected_entries()
    actual = registry(CATALOG_ROOT, tmp_path, archive, entries=entries)
    augmented = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    contracts = catalog_configuration_contracts(augmented, deployment_runtime_entries=entries)
    for model_id, entry in entries.items():
        model = actual.get(model_id, require_enabled=False)
        assert not model.enabled
        assert model.gateway.qualification["runtime_origin"]["variant_id"] == entry["variant_id"]
        assert model.gateway.qualification["states"]["runtime_ready"]
        assert model.gateway.qualification["states"]["semantic_qualified"]
        assert not model.gateway.qualification["states"]["elasticity_qualified"]
        assert not model.gateway.qualification["states"]["http_mcp_qualified"]
        assert contracts[model_id].runtime_image_digest == entry["record"]["runtime"]["image"]["digest"]
        assert model.readiness_probe.path == "/v1/health/ready"


def test_original_binding_validation_precedes_native_projection(archive, native_tree, tmp_path):
    (native_tree / "native/phenoage.json").write_text("{}")
    with pytest.raises(RegistryError) as caught:
        registry(native_tree, tmp_path, archive, binding_digest=hashlib.sha256(b"other-catalog").hexdigest())
    assert isinstance(caught.value.__cause__, CatalogError)
    assert str(caught.value.__cause__) == "serving bindings catalog digest mismatch"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (("schema",), "other/v1", "unsupported native"),
        (("record", "model", "id"), "molmim", "alias or replace"),
        (("variant_id",), "molmim-exact-weights-portable", "alias or replace"),
        (("runtime_architecture",), "cuda", "architecture differs"),
        (("runtime_architecture",), "blackwell-sm103", "cpu or cuda"),
        (("record", "support", "route_exposed"), True, "claims static routing"),
        (("record", "semantic_validator", "fixture_sha256"), hashlib.sha256(b"wrong").hexdigest(), "digest mismatch"),
        (("semantic_requests", "serialization"), "unspecified", "exact request contract"),
        (("semantic_requests", "invocation", "endpoint"), "/other", "invocation differs"),
        (("semantic_requests", "requests"), [], "requires two requests"),
        (("artifact_manifest", "path"), "../../../outside.json", "escapes the catalog"),
        (("artifact_manifest", "sha256"), hashlib.sha256(b"wrong").hexdigest(), "reference digest mismatch"),
        (("record", "cache", "artifact", "expanded_bytes"), 2300, "capacity bound"),
    ],
)
def test_invalid_native_contract_is_not_loaded(archive, native_tree, field, value, message):
    item = declaration(native_tree)
    target = item
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    write_declaration(native_tree, item)
    with pytest.raises((CatalogError, DeploymentRuntimeError), match=message):
        augment_native_catalog(archive, native_tree, repo_root=REPO_ROOT)


def test_repeated_request_payloads_are_not_semantic_qualification(archive, native_tree):
    item = declaration(native_tree)
    item["semantic_requests"]["requests"][1]["payload_sha256"] = item["semantic_requests"]["requests"][0][
        "payload_sha256"
    ]
    write_declaration(native_tree, item)
    with pytest.raises(CatalogError, match="requests must be distinct"):
        augment_native_catalog(archive, native_tree, repo_root=REPO_ROOT)


def test_manifest_source_cannot_alias_another_revision_even_if_rehashed(archive, native_tree):
    item = declaration(native_tree)
    path = native_tree / "native" / item["artifact_manifest"]["path"]
    value = json.loads(path.read_text())
    value["source"]["revision"] = "different-source"
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    path.write_bytes(_canonical_bytes(value))
    item["artifact_manifest"]["sha256"] = digest
    item["record"]["cache"]["artifact"]["manifest_digest"] = digest
    write_declaration(native_tree, item)
    with pytest.raises(CatalogError, match="exact model/source contract"):
        augment_native_catalog(archive, native_tree, repo_root=REPO_ROOT)


def test_selected_native_runtime_must_match_canonical_source(archive, tmp_path):
    augmented = augment_native_catalog(archive, CATALOG_ROOT, repo_root=REPO_ROOT)
    bindings = ServingBindings(archive.digest, MappingProxyType({}))
    entries = copy.deepcopy(selected_entries())
    entries["phenoage"]["record"]["model"]["source"]["revision"] = "other-revision"
    path = tmp_path / "selected.json"
    path.write_text(json.dumps({"schema": SET_SCHEMA, "models": entries}))
    with pytest.raises(DeploymentRuntimeError, match="differs from exact canonical variant"):
        bind_deployment_runtimes(
            bind_gateway_catalog(augmented, bindings), augmented, bindings, path, catalog_dir=CATALOG_ROOT
        )
