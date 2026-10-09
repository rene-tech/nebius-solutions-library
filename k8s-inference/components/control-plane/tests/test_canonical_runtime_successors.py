from __future__ import annotations

import copy
import hashlib
import json

import pytest
import test_deployment_runtimes as shared_tests
from conftest import CATALOG_ROOT

from fs2_serve.deployment_runtimes import (
    DeploymentRuntimeError,
    _canonical_service_contract,
    _record,
    deployment_runtime_model_schema,
)
from fs2_serve.qualification import _policy, _runtime_origin

project = shared_tests.project


@pytest.fixture
def inputs(tmp_path):
    return shared_tests.inputs.__wrapped__(tmp_path)


def successor(catalog, template):
    model_id = "cosmos3-nano"
    record = catalog.model(model_id).to_dict()
    image_hash = hashlib.sha256(b"canonical successor runtime fixture").hexdigest()
    record["runtime"]["image"] = {
        "reference": "registry.test/cosmos@sha256:" + image_hash,
        "digest": "sha256:" + image_hash,
        "state": "resolved",
    }
    record["resources"]["gpu"].update({"class": "NVIDIA-H100-SXM5-80GB", "b300_state": "unverified"})
    row = copy.deepcopy(template["qualification"])
    row.update(
        model_id=model_id,
        variant_id=None,
        policy=_policy(catalog, model_id),
        runtime_origin=_runtime_origin(catalog, model_id, None),
    )
    row["active_runtime"] = {
        "model_revision": record["model"]["source"]["revision"],
        "runtime_image_digest": record["runtime"]["image"]["digest"],
        "service": {"namespace": "fs2-models", "name": model_id, "port": 8080},
    }
    return {
        "schema": template["schema"],
        "model_id": model_id,
        "variant_id": None,
        "record": record,
        "qualification": row,
    }


def test_exact_canonical_successor_uses_reviewed_service_without_granting_a_route(inputs):
    entry = successor(inputs[0], inputs[3]["genmol"])
    entries = {**inputs[3], "cosmos3-nano": entry}
    projected = project(inputs, entries)
    model = projected.model("cosmos3-nano")
    assert model.runtime_image_digest == entry["record"]["runtime"]["image"]["digest"]
    assert model.qualification["active_runtime"]["service"]["port"] == 8080
    assert not model.routable and not model.mcp_invocable
    assert projected.model("genmol").runtime_image_digest == inputs[3]["genmol"]["record"]["runtime"]["image"]["digest"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model", "id"), "genmol"),
        (("model", "source", "repository"), "other/model"),
        (("model", "source", "revision"), "b" * 40),
        (("model", "source", "kind"), "ngc-nim"),
        (("model", "source", "license", "id"), "MIT"),
        (("cache", "artifact", "manifest_digest"), "c" * 64),
        (("cache", "artifact", "expanded_bytes"), 1234),
        (("cache", "owner"), "runtime-image"),
        (("interface", "policy", "non_clinical"), True),
        (("runtime", "kind"), "nim"),
    ],
)
def test_successor_cannot_substitute_source_weights_model_or_policy(inputs, path, value):
    entry = successor(inputs[0], inputs[3]["genmol"])
    target = entry["record"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DeploymentRuntimeError):
        project(inputs, {"cosmos3-nano": entry})


@pytest.mark.parametrize(
    ("field", "value"),
    [("namespace", "other"), ("name", "genmol"), ("name", "cosmos3-nano-b300"), ("port", 8000), ("port", 9999)],
)
def test_successor_cannot_choose_its_service_or_port(inputs, field, value):
    entry = successor(inputs[0], inputs[3]["genmol"])
    entry["qualification"]["active_runtime"]["service"][field] = value
    with pytest.raises(DeploymentRuntimeError, match="aliases another model"):
        project(inputs, {"cosmos3-nano": entry})


def test_another_model_cannot_drop_its_variant_to_bypass_validation(inputs):
    entries = copy.deepcopy(inputs[3])
    entries["genmol"]["variant_id"] = None
    with pytest.raises(DeploymentRuntimeError, match="no reviewed service contract"):
        project(inputs, entries)


def test_canonical_record_requires_an_independent_reviewed_contract(inputs):
    entry = successor(inputs[0], inputs[3]["genmol"])
    with pytest.raises(DeploymentRuntimeError, match="reviewed archival contract"):
        _record(entry["record"], "cosmos3-nano", None, inputs[0], deployment_runtime_model_schema(CATALOG_ROOT))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model_digest",), "d" * 64),
        (("source", "repository"), "other/model"),
        (("source", "revision"), "a" * 40),
        (("service", "namespace"), "other"),
        (("service", "name"), "genmol"),
        (("service", "port"), True),
    ],
)
def test_reviewed_contract_is_bound_to_archival_identity_and_model_owned_service(inputs, tmp_path, path, value):
    document = json.loads((CATALOG_ROOT / "contracts/canonical-runtime-services.json").read_text())
    target = document["models"]["cosmos3-nano"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts/canonical-runtime-services.json").write_text(json.dumps(document))
    with pytest.raises(DeploymentRuntimeError):
        _canonical_service_contract(inputs[0], tmp_path, "cosmos3-nano")


def test_even_an_exact_nim_contract_cannot_use_the_canonical_successor_path(inputs, tmp_path):
    original = inputs[0].model("genmol")
    record = original.to_dict()
    assert record["runtime"]["kind"] == "nim"
    document = {
        "schema": "fs2-serve.nebius.ai/canonical-runtime-services/v1",
        "models": {
            "genmol": {
                "model_digest": original.digest,
                "source": {key: record["model"]["source"][key] for key in ("kind", "repository", "revision")},
                "service": {"namespace": "fs2-models", "name": "genmol", "port": 8000},
            }
        },
    }
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts/canonical-runtime-services.json").write_text(json.dumps(document))
    with pytest.raises(DeploymentRuntimeError, match="non-NIM archival identity"):
        _canonical_service_contract(inputs[0], tmp_path, "genmol")
