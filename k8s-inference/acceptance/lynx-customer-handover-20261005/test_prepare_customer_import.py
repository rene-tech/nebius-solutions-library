"""Offline packaging/lineage tests, not native MD or performance qualification."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("customer_import", Path(__file__).with_name("prepare_customer_import.py"))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True))
    return path


@pytest.fixture
def fixture(tmp_path):
    operation = "bd185adb-2f78-4ce4-870b-ed26608b9d32"
    engine = "example.invalid/gromacs@sha256:" + "1" * 64
    parameters = helper.normalize({
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "jobs": [{"id": "customer-md", "steps": [
            {"id": "prepare", "command": "grompp", "args": ["-o", "original.tpr"]},
            {"id": "production", "command": "mdrun",
             "args": ["-s", "original.tpr", "-deffnm", "md", "-nb", "auto"]},
            {"id": "check-output", "command": "check", "args": ["-f", "md.xtc"]},
        ]}], "max_wall_seconds": 604800,
    })
    native = tmp_path / "native"
    native.mkdir()
    contents = {"original.tpr": b"unit-fixture-not-real-science",
                "fs2-production.cpt": b"unit-native-checkpoint",
                "md.part0023.xtc": b"prior trajectory history",
                "fs2-production-segment-000001.log": b"original wrapper log"}
    files = []
    for name, content in contents.items():
        (native / name).write_bytes(content)
        files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    tpr_sha = hashlib.sha256(contents["original.tpr"]).hexdigest()
    checkpoint = {
        "state": {"operation_id": operation, "job_id": "customer-md", "generation": 23,
                  "completed_steps": ["prepare"],
                  "commands": [{"step_id": "production", "checkpoint_step": 17000000}],
                  "active_step": {"id": "production", "tpr_sha256": tpr_sha, "target_step": 500000000},
                  "recipe_sha256": hashlib.sha256(helper.canonical(
                      {"request": parameters, "job": "customer-md", "image": engine})).hexdigest()},
        "files": files,
    }
    raw = helper.canonical(checkpoint)
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_bytes(raw)
    choices = {"operation_id": operation, "model_id": "gromacs", "status": "failed",
               "jobs": [{"job_id": "customer-md", "checkpoint": {
                   "artifact_id": "016a642d-b7b8-4d3f-8803-48f7d366c4bc",
                   "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
               }}]}
    tuning = json.loads(Path(__file__).with_name("tuning.example.json").read_text())
    args = SimpleNamespace(
        source_operation=operation, job_id="customer-md", source_engine_id=engine,
        expected_tpr_sha256=tpr_sha, expected_target_step=500000000,
        checkpoint_choices=write_json(tmp_path / "choices.json", choices), checkpoint=checkpoint_file,
        original_parameters=write_json(tmp_path / "original.json", parameters), native_root=native,
        tuning=write_json(tmp_path / "tuning.json", tuning), output=tmp_path / "prepared",
        max_output_bytes=4 * 1024**3, output_prefix="runs/customer-tuned-import",
    )
    return args, choices, checkpoint, parameters, contents


def test_complete_history_and_full_target_preserved_with_deterministic_bundle(fixture):
    args, _, checkpoint, original, contents = fixture
    result = helper.prepare(args)
    assert result["prepared"] and not result["submitted"]
    assert result["saved_step"] == 17000000 and result["full_target_step"] == 500000000
    parameters = json.loads((args.output / "parameters.json").read_text())
    assert parameters["max_wall_seconds"] == 1209600
    assert parameters["jobs"][0]["steps"][1] == original["jobs"][0]["steps"][2]
    production = parameters["jobs"][0]["steps"][0]
    assert production["restart_checkpoint"] == "fs2-production.cpt"
    assert production["args"][:4] == ["-s", "original.tpr", "-deffnm", "md"]
    assert "convert-tpr" not in [step["command"] for step in parameters["jobs"][0]["steps"]]
    provenance = json.loads((args.output / "provenance.json").read_text())
    history = provenance["history_prefix"]
    with tarfile.open(args.output / "input.tar.gz", "r:gz") as archive:
        for name, content in contents.items():
            assert archive.extractfile(name).read() == content
            assert archive.extractfile(f"{history}/{name}").read() == content
        assert archive.extractfile(f"{history}/source-checkpoint.json").read() == helper.canonical(checkpoint)
    args.output = args.output.with_name("prepared-again")
    replay = helper.prepare(args)
    assert replay == result


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_cannot_import_an_active_or_completed_source(fixture, status):
    args, choices, *_ = fixture
    choices["status"] = status
    write_json(args.checkpoint_choices, choices)
    with pytest.raises(ValueError, match="not terminal"):
        helper.prepare(args)
    assert not args.output.exists()


def test_rejects_old_checkpoint_after_choices_report_a_newer_one(fixture):
    args, choices, *_ = fixture
    choices["jobs"][0]["checkpoint"]["sha256"] = "0" * 64
    write_json(args.checkpoint_choices, choices)
    with pytest.raises(ValueError, match="latest public checkpoint"):
        helper.prepare(args)


def test_original_parameters_must_match_frozen_recipe(fixture):
    args, _, _, original, _ = fixture
    altered = copy.deepcopy(original)
    altered["jobs"][0]["steps"][1]["args"] += ["-rdd", "99"]
    write_json(args.original_parameters, altered)
    with pytest.raises(ValueError, match="frozen source recipe"):
        helper.prepare(args)


@pytest.mark.parametrize("field,value", [("expected_tpr_sha256", "0" * 64), ("expected_target_step", 18000000)])
def test_does_not_silently_change_scientific_tpr_or_full_target(fixture, field, value):
    args, *_ = fixture
    setattr(args, field, value)
    with pytest.raises(ValueError, match="full scientific target"):
        helper.prepare(args)


def test_damaged_or_missing_history_is_not_silently_dropped(fixture):
    args, *_ = fixture
    (args.native_root / "md.part0023.xtc").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="Copied native file differs"):
        helper.prepare(args)


def test_tuning_cannot_override_scientific_options(fixture):
    args, *_ = fixture
    tuning = json.loads(args.tuning.read_text())
    tuning["mdrun"]["-nsteps"] = "100"
    write_json(args.tuning, tuning)
    with pytest.raises(ValueError, match="performance-only"):
        helper.prepare(args)


def test_complete_import_cannot_overflow_workspace(fixture):
    args, choices, checkpoint, *_ = fixture
    content = b"x" * (600 * 1024)
    args.max_output_bytes = 1024**2  # Valid minimum, smaller than the complete duplicated history.
    path = args.native_root / "md.part0023.xtc"
    path.write_bytes(content)
    row = next(item for item in checkpoint["files"] if item["path"] == path.name)
    row.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    raw = helper.canonical(checkpoint)
    args.checkpoint.write_bytes(raw)
    choices["jobs"][0]["checkpoint"].update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    write_json(args.checkpoint_choices, choices)
    with pytest.raises(ValueError, match="workspace bytes"):
        helper.prepare(args)


def test_examples_validate_against_current_public_native_contract():
    folder = Path(__file__).parent
    request = json.loads((folder / "import-request.example.json").read_text())
    assert helper.normalize(request["parameters"])["max_wall_seconds"] == 1209600
    from fs2_serve.scientific_batch.gromacs_resume import GromacsResumeRequest
    parsed = GromacsResumeRequest.model_validate_json((folder / "resume-request.example.json").read_text())
    assert parsed.job_id == "production" and parsed.max_wall_seconds == 1209600
