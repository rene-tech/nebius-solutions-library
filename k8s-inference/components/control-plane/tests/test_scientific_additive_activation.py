from __future__ import annotations

import copy
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import pytest
from conftest import CATALOG_ROOT

from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile
from fs2_serve.scientific_batch.service import ScientificBatchService


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def additive_fixture():
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    root = json.loads((CATALOG_ROOT / "contracts/scientific-execution-map.json").read_text())
    # Measure a real existing model with every execution-relevant field intact.
    legacy = root["models"][0]
    old_map = {"schema": root["schema"], "models": [legacy]}
    digest = hashlib.sha256(canonical(old_map)).hexdigest()
    old_profile = copy.deepcopy(dict(catalog.get(legacy["model_id"]).value))
    old_profile["qualification"]["execution_map_sha256"] = digest
    new_profile = copy.deepcopy(old_profile)
    new_profile["model_id"] = "additive-test-model"
    new_row = copy.deepcopy(legacy)
    new_row["model_id"] = new_profile["model_id"]
    profiles = ScientificProfileCatalog(
        profiles={
            value["model_id"]: ScientificWorkloadProfile(MappingProxyType(value))
            for value in (old_profile, new_profile)
        },
        validators=catalog._validators,
    )
    document = {
        "schema": root["schema"],
        "models": [legacy, new_row],
        "qualification_baselines": {digest: [legacy["model_id"]]},
    }
    return profiles, document, digest


def render(tmp_path, profiles, document):
    path = tmp_path / "map.json"
    path.write_bytes(canonical(document))
    return FileScientificManifestRenderer(path=path, profiles=profiles)


def test_additive_map_retains_exact_legacy_proof_without_qualifying_new_model(tmp_path):
    profiles, document, digest = additive_fixture()
    renderer = render(tmp_path, profiles, document)
    old_id = document["models"][0]["model_id"]
    assert renderer.qualification_matches(old_id, "sha256:" + digest)
    assert not renderer.qualification_matches("additive-test-model", "sha256:" + digest)
    full = {key: value for key, value in document.items() if key != "qualification_baselines"}
    assert renderer.execution_map_sha256 == "sha256:" + hashlib.sha256(canonical(full)).hexdigest()
    assert renderer.execution_map_sha256 != "sha256:" + digest
    # Discovery cannot lose this sibling merely because another model arrived.
    service = object.__new__(ScientificBatchService)
    service.profiles = profiles
    service.execution_binding = renderer
    service.scheduling = SimpleNamespace(freeze=lambda **kwargs: None)
    assert [
        entry.model_id
        for entry in service.discovery_profiles(
            tenant_id="tenant-a",
            allowed_models=frozenset({old_id}),
            surface="mcp",
        )
    ] == [old_id]


@pytest.mark.parametrize("mutation", ["remove", "environment", "image", "namespace", "identity"])
def test_historical_proof_never_accepts_changed_or_missing_legacy_row(tmp_path, mutation):
    profiles, document, _ = additive_fixture()
    row = document["models"][0]
    if mutation == "remove":
        document["models"].pop(0)
    elif mutation == "environment":
        row["stages"][0]["environment"]["CHANGED"] = "yes"
    elif mutation == "image":
        row["stages"][0]["image"] = "runtime@sha256:" + "f" * 64
    elif mutation == "namespace":
        row["workload_namespace"] = "different"
    else:
        row["execution_identity_sha256"] = "f" * 64
    with pytest.raises(ScientificExecutionMapError, match="qualification baseline"):
        render(tmp_path, profiles, document)
