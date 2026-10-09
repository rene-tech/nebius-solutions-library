import hashlib
import io
import json
import tarfile

import pytest

from fs2_amber import ENGINE_ID, PARAMETER_SCHEMA, RESULT_SCHEMA
from fs2_amber.contracts import canonical, normalize
from fs2_gromacs.files import digest_file, inventory
from audit_hosted_identity import audit


def put(path, value):
    path.write_text(json.dumps(value) + "\n")


def change(path, mutate):
    value = json.loads(path.read_text())
    mutate(value)
    put(path, value)


@pytest.fixture
def cohort(tmp_path):
    fixture, work, receipt = (tmp_path / name for name in ("fixture", "workspace", "receipt"))
    data = work / "data"
    for directory in (fixture, data, receipt):
        directory.mkdir(parents=True)
    (data / "input.cpptraj").write_text("run\n")
    (data / "analysis.dat").write_text("finite scientific result placeholder\n")
    bundle = fixture / "input.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(data / "input.cpptraj", arcname="input.cpptraj")
    request = {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "rep-1", "steps": [
        {"id": "analyze", "kind": "cpptraj", "input": "input.cpptraj", "expected_outputs": ["analysis.dat"]}
    ]}]}
    for path in (fixture / "request.json", work / "request.json"):
        put(path, request)
    recipe = hashlib.sha256(canonical({"request": normalize(request), "job": "rep-1", "image": ENGINE_ID})).hexdigest()
    put(work / "result.json", {
        "schema": RESULT_SCHEMA, "status": "succeeded", "engine_id": ENGINE_ID,
        "operation_id": "operation-1", "job_id": "rep-1", "recipe_sha256": recipe,
        "completed_steps": ["analyze"], "commands": [{"step_id": "analyze", "exit_code": 0}],
        "files": inventory(data, max_bytes=1_000_000),
    })
    put(receipt / "receipt.json", {"state": "verified", "operation_id": "operation-1",
        "identity": {"source_sha256": digest_file(bundle), "caller_fingerprint": "never-publish-me"}})
    put(receipt / "status.json", {"operation": {"id": "operation-1", "status": "succeeded", "principal_id": "private-principal"},
        "batch": {"result_published": True, "stages": [{"attempts": [{"resource_released": True}]}]}})
    put(receipt / "output-manifest.json", {"entries": []})
    return fixture, work, receipt


def test_passed_identity_is_not_a_scientific_claim_or_identity_leak(cohort):
    result = audit("amber", *cohort)
    assert result["status"] == "passed"
    assert result["completed_steps"] == ["analyze"]
    assert len(result["immutable_input_files"]) == 1
    assert not result["scientific_validation_performed_by_this_audit"]
    assert "never-publish-me" not in json.dumps(result)
    assert "private-principal" not in json.dumps(result)


@pytest.mark.parametrize("filename,mutate,match", [
    ("result", lambda r: r.update(recipe_sha256="0" * 64), "recipe"),
    ("result", lambda r: r.update(engine_id="different-engine"), "exact native engine"),
    ("result", lambda r: r.update(completed_steps=[]), "ordered stage"),
    ("result", lambda r: r.update(operation_id="different-operation"), "identities disagree"),
    ("result", lambda r: r["commands"][0].update(exit_code=1), "native commands"),
    ("receipt", lambda r: r.update(state="running"), "verified customer"),
    ("receipt", lambda r: r["identity"].update(source_sha256="0" * 64), "different immutable"),
    ("status", lambda r: r["batch"]["stages"][0]["attempts"][0].update(resource_released=False), "owns compute"),
])
def test_rejects_identity_or_completion_mutation(cohort, filename, mutate, match):
    _, work, receipt = cohort
    path = work / "result.json" if filename == "result" else receipt / (filename + ".json")
    change(path, mutate)
    with pytest.raises(ValueError, match=match):
        audit("amber", *cohort)


def test_rejects_changed_materialized_request(cohort):
    change(cohort[1] / "request.json", lambda r: r.update(threads=2))
    with pytest.raises(ValueError, match="frozen fixture"):
        audit("amber", *cohort)


def test_rejects_unhashed_native_artifact_mutation(cohort):
    (cohort[1] / "data/analysis.dat").write_text("changed\n")
    with pytest.raises(ValueError, match="inventory"):
        audit("amber", *cohort)


def test_rejects_rehashed_immutable_input_mutation(cohort):
    data = cohort[1] / "data"
    (data / "input.cpptraj").write_text("different native science\n")
    change(cohort[1] / "result.json", lambda r: r.update(files=inventory(data, max_bytes=1_000_000)))
    with pytest.raises(ValueError, match="uploaded bytes"):
        audit("amber", *cohort)


@pytest.mark.parametrize("kind", ["empty", "duplicate", "symlink"])
def test_rejects_ambiguous_archive(cohort, kind):
    bundle = cohort[0] / "input.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        if kind == "duplicate":
            for _ in range(2):
                member = tarfile.TarInfo("input.cpptraj")
                member.size = 4
                archive.addfile(member, io.BytesIO(b"run\n"))
        elif kind == "symlink":
            member = tarfile.TarInfo("input.cpptraj")
            member.type = tarfile.SYMTYPE
            member.linkname = "analysis.dat"
            archive.addfile(member)
    change(cohort[2] / "receipt.json", lambda r: r["identity"].update(source_sha256=digest_file(bundle)))
    with pytest.raises(ValueError, match="empty|non-regular, duplicate"):
        audit("amber", *cohort)
