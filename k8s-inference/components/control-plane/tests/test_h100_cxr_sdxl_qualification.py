from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fs2_serve_catalog.loader import load_catalog

import fs2_serve.configuration as configuration

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "catalog/runtime"
CLASS = "nvidia-h100-sxm5-80gb"
NAME = "h100-cxr-sdxl-20260919.json"
DIGEST = "65b682a6e4c84d95965665816d6247c478165a07c860524b1a132e3fce684ef3"


def catalog():
    return load_catalog(CATALOG, repo_root=CATALOG / "packaged-repository")


def test_exact_packaged_receipt_and_public_evidence_are_content_addressed():
    source = ROOT / "catalog/profiles/evidence/h100-cxr-sdxl-runtime-qualification-20260919.json"
    packaged = Path(configuration.__file__).parent / "runtime_qualifications" / NAME
    assert packaged.read_bytes() == source.read_bytes()
    assert hashlib.sha256(packaged.read_bytes()).hexdigest() == DIGEST
    receipt = configuration._load_reviewed_runtime_qualification(NAME, DIGEST)
    proof = json.loads((ROOT / "acceptance/h100-cxr-sdxl-placement-20260919/retained-public-proof.json").read_bytes())
    assert (
        hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        == receipt["source_evidence"]["retained_public_placement_sha256"]
    )
    assert {row["model_id"]: row["qualified_requests"] for row in receipt["models"]} == {
        "sdxl": 35,
        "nv-reason-cxr-3b": 18,
    }
    assert len(proof["models"]["sdxl"]["unmatched"]) == 1
    assert len(proof["models"]["nv-reason-cxr-3b"]["unmatched"]) == 2


def test_built_wheel_contains_exact_readable_qualification(tmp_path):
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(  # noqa: S603 - fixed local build command, temporary test output.
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=120,
    )
    [wheel] = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        name = "fs2_serve/runtime_qualifications/" + NAME
        assert hashlib.sha256(archive.read(name)).hexdigest() == DIGEST
        assert (archive.getinfo(name).external_attr >> 16) & 0o004


def test_only_two_proven_placements_change_no_immutable_model_identity(monkeypatch):
    source = catalog()
    after = configuration.catalog_configuration_contracts(source)
    monkeypatch.setattr(
        configuration, "_reviewed_runtime_qualifications", lambda: (configuration._reviewed_runtime_qualification(),)
    )
    before = configuration.catalog_configuration_contracts(source)
    changed = {model for model in before if before[model] != after[model]}
    assert changed == {"sdxl", "nv-reason-cxr-3b"}
    for model in changed:
        assert after[model].supported_accelerator_classes == before[model].supported_accelerator_classes | {CLASS}
        assert (
            replace(after[model], supported_accelerator_classes=before[model].supported_accelerator_classes)
            == before[model]
        )
        assert "nvidia-l40s-48gb" not in after[model].supported_accelerator_classes


@pytest.mark.parametrize("model", ["sdxl", "nv-reason-cxr-3b"])
@pytest.mark.parametrize("fault", ["revision", "image", "manifest", "gpu_count", "topology"])
def test_changed_runtime_or_geometry_not_qualified(model, fault):
    source = catalog()
    record = source.records[model]
    value = record.to_dict()
    if fault == "revision":
        value["model"]["source"]["revision"] = "different"
    elif fault == "image":
        value["runtime"]["image"]["digest"] = "sha256:" + "a" * 64
    elif fault == "manifest":
        value["cache"]["artifact"]["manifest_digest"] = "a" * 64
    elif fault == "gpu_count":
        value["resources"]["gpu"]["count"] = 2
    else:
        value["resources"]["gpu"]["topology"] = "multi-gpu"
    source = replace(source, records={**source.records, model: replace(record, _value=value)})
    assert CLASS not in configuration.catalog_configuration_contracts(source)[model].supported_accelerator_classes


def test_modified_packaged_receipt_is_not_trusted(monkeypatch):
    monkeypatch.setattr(Path, "read_bytes", lambda _: b"{}")
    assert configuration._load_reviewed_runtime_qualification(NAME, DIGEST) == {}


def test_conflicting_content_addressed_receipts_fail_closed(monkeypatch):
    old = configuration._reviewed_runtime_qualification()
    changed = copy.deepcopy(old)
    changed["models"][0]["semantic_qualified"] = False
    monkeypatch.setattr(configuration, "_reviewed_runtime_qualification", lambda: old)
    monkeypatch.setattr(configuration, "_load_reviewed_runtime_qualification", lambda *_: changed)
    assert configuration._reviewed_runtime_qualifications() == ()


@pytest.mark.parametrize("fault", ["authority", "gpu_class", "duplicate", "missing_flag", "truthy_string"])
def test_invalid_receipt_structure_cannot_add_a_grant(monkeypatch, fault):
    value = configuration._load_reviewed_runtime_qualification(NAME, DIGEST)
    if fault == "authority":
        value["authority"] = "unreviewed"
    elif fault == "gpu_class":
        value["accelerator"]["class"] = "nvidia-l40s-48gb"
    elif fault == "duplicate":
        value["models"].append(copy.deepcopy(value["models"][0]))
    elif fault == "missing_flag":
        del value["models"][0]["runtime_ready"]
    else:
        value["models"][0]["runtime_ready"] = "false"
    raw = json.dumps(value).encode()
    monkeypatch.setattr(Path, "read_bytes", lambda _: raw)
    assert configuration._load_reviewed_runtime_qualification(NAME, hashlib.sha256(raw).hexdigest()) == {}
