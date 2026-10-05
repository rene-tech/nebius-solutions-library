import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from prepare_acceptance import parameters, target_step, validate_source  # noqa: E402
from validate_resume import (  # noqa: E402
    delivery_gate, preserved_history, restart_step, topology_equivalence, verify_customer_storage,
)


def source(tmp_path, monkeypatch):
    import prepare_acceptance
    contents = {"simulation.tpr": b"exact TPR", "fs2-production.cpt": b"native checkpoint",
                "md.part0070.xtc": b"trajectory", "fs2-production-segment-000001.log": b"old wrapper log"}
    rows = []
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
        rows.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
    monkeypatch.setattr(prepare_acceptance, "TPR_SHA256", rows[0]["sha256"])
    return {"schema": "fs2-serve.nebius.ai/gromacs-customer-checkpoint/v1", "files": rows,
            "state": {"operation_id": prepare_acceptance.SOURCE_OPERATION, "generation": 71,
                      "commands": [{"checkpoint_step": 13963440}],
                      "active_step": {"tpr_sha256": rows[0]["sha256"]}}}


def test_source_requires_all_actual_bytes_and_history(tmp_path, monkeypatch):
    manifest = source(tmp_path, monkeypatch)
    files = validate_source(manifest, tmp_path, tpr_path="simulation.tpr", checkpoint_path="fs2-production.cpt")
    assert len(files) == 4
    (tmp_path / "md.part0070.xtc").write_bytes(b"corruption")
    with pytest.raises(ValueError, match="SHA-256"):
        validate_source(manifest, tmp_path, tpr_path="simulation.tpr", checkpoint_path="fs2-production.cpt")


@pytest.mark.parametrize("damage", ["owner", "step", "generation", "duplicate", "path", "symlink", "tpr"])
def test_import_rejects_ambiguous_or_changed_source(tmp_path, monkeypatch, damage):
    manifest = source(tmp_path, monkeypatch)
    if damage == "owner":
        manifest["state"]["operation_id"] = "another-owner-operation"
    elif damage == "step":
        manifest["state"]["commands"][0]["checkpoint_step"] = 0
    elif damage == "generation":
        manifest["state"]["generation"] = 70
    elif damage == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif damage == "path":
        manifest["files"][0]["path"] = "../foreign.tpr"
    elif damage == "symlink":
        path = tmp_path / "fs2-production.cpt"
        path.unlink()
        path.symlink_to(tmp_path / "simulation.tpr")
    else:
        manifest["state"]["active_step"]["tpr_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_source(manifest, tmp_path, tpr_path="simulation.tpr", checkpoint_path="fs2-production.cpt")


def test_recipe_only_changes_finite_horizon_and_explicit_tuning():
    value, target = parameters(tpr_path="simulation.tpr", checkpoint_path="fs2-production.cpt",
                              additional_ns=60, label="long", nstlist=200)
    assert target == 43963440
    steps = value["jobs"][0]["steps"]
    assert steps[0]["args"][-2:] == ["-nsteps", "43963440"]
    assert steps[1]["args"][-4:] == ["-tol", "0", "-abstol", "0"]
    native = steps[2]
    assert native["restart_checkpoint"] == "source-history/fs2-production.cpt"
    assert "-nsteps" not in native["args"]
    assert value["segment_minutes"] == value["checkpoint_minutes"] == 5
    assert value["max_wall_seconds"] == 300
    assert all(step["command"] not in {"grompp", "pdb2gmx", "genion"} for step in steps)
    for step in steps:
        if step["command"] in {"trjcat", "eneconv"}:
            assert step["args"][1]["nonempty"] is True
    assert target_step(2) == (14963440, 14963440)


@pytest.mark.parametrize("duration", [0, -1, "NaN", "Infinity", "0.0000001", 1000])
def test_horizon_must_be_finite_exact_and_inside_original_target(duration):
    with pytest.raises(ValueError):
        target_step(duration)


def test_fixture_is_deterministic_and_retains_wrapper_logs(tmp_path, monkeypatch):
    from prepare_acceptance import prepare, sha
    native = tmp_path / "native"
    native.mkdir()
    manifest = source(native, monkeypatch)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    args = SimpleNamespace(source_manifest=path, source_manifest_sha256=sha(path), native_root=native,
        tpr_path="simulation.tpr", checkpoint_path="fs2-production.cpt", additional_ns=2, label="short",
        nstlist=200, threads=8, pin="auto", bootstrap_seconds=300, max_output_bytes=4 * 1024**3,
        minimum_native_seconds=0, minimum_delivered_ns_per_day=200)
    args.output = tmp_path / "first"
    prepare(args)
    args.output = tmp_path / "second"
    prepare(args)
    assert sha(tmp_path / "first/input.tar.gz") == sha(tmp_path / "second/input.tar.gz")
    import tarfile
    with tarfile.open(tmp_path / "first/input.tar.gz") as archive:
        assert "source-history/fs2-production-segment-000001.log" in archive.getnames()
        assert archive.extractfile("source-history/simulation.tpr").read() == b"exact TPR"


def comparison(target=14963440):
    sections = ["inputrec", "mtop topology", "force field parameters", "atoms", "InteractionLists",
                "molecule blocks", "groups", "intermolecular exclusions", "moleculeBlockIndices",
                "flags", "box", "box_rel", "boxv", "x", "v"]
    return "gmx check\n" + "\n".join("comparing " + name for name in sections) + f"\ninputrec->nsteps (500000000 - {target})\n"


def test_full_native_tpr_comparison_accepts_only_requested_nsteps():
    assert topology_equivalence(comparison(), 14963440)["status"] == "passed"
    for text in (comparison() + "inputrec->dt (0.002 - 0.004)\n",
                 comparison().replace("comparing v\n", ""),
                 comparison(14963441), comparison() + "x[0] (1.0 - 2.0)\n"):
        with pytest.raises(ValueError):
            topology_equivalence(text, 14963440)


def test_native_quote_cannot_hide_a_scientific_difference():
    text = comparison() + '\nGROMACS reminds you: "quote" (name)\n'
    assert topology_equivalence(text, 14963440)["status"] == "passed"
    with pytest.raises(ValueError):
        topology_equivalence(text + "inputrec->delta-t (0.002 - 0.003)\n", 14963440)


def test_native_empty_part_probe_inventory_preserves_zeros_and_rejects_links(tmp_path):
    from qualify_empty_segments import inventory
    (tmp_path / "empty.xtc").touch()
    (tmp_path / "frame.xtc").write_bytes(b"one frame")
    result = inventory(tmp_path)
    assert result["empty.xtc"] == {"size_bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    assert result["frame.xtc"]["size_bytes"] == 9
    (tmp_path / "link.xtc").symlink_to(tmp_path / "empty.xtc")
    with pytest.raises(ValueError, match="contained regular files"):
        inventory(tmp_path)


@pytest.mark.parametrize("changed", [None, "tenant-id", "model-id", "operation-id"])
def test_hardware_observer_can_only_read_the_exact_demo_operation(changed):
    from observe_demo import owned_pods
    labels = {"fs2.nebius.ai/operation-id": "task-owned", "fs2.nebius.ai/tenant-id": "demo-user",
              "fs2.nebius.ai/model-id": "gromacs"}
    pod = {"metadata": {"labels": labels}}
    if changed:
        labels["fs2.nebius.ai/" + changed] = "not-this-task"
        with pytest.raises(ValueError, match="outside this exact demo operation"):
            owned_pods({"items": [pod]}, "task-owned")
    else:
        assert owned_pods({"items": [pod]}, "task-owned") == [pod]


def test_history_and_late_resume_are_checked_independently():
    fixture = {"source_files": [{"path": "one.xtc", "sha256": "a", "size_bytes": 10}]}
    checkpoint = {"files": [{"path": "source-history/one.xtc", "sha256": "a", "size_bytes": 10}]}
    assert preserved_history(fixture, checkpoint) == 1
    checkpoint["files"][0]["sha256"] = "b"
    with pytest.raises(ValueError):
        preserved_history(fixture, checkpoint)
    assert restart_step("continuing from step 13963440, 27926.88 ps") == 13963440
    with pytest.raises(ValueError):
        restart_step("started from coordinates")


def test_delivered_rate_uses_new_work_and_whole_public_wall_not_native_counters():
    fixture = {"target_step": 43963440, "dt_ps": "0.002", "minimum_native_seconds": 21600,
               "minimum_delivered_ns_per_day": 200}
    final = {"operation": {"accepted_at": "2026-10-05T00:00:00+00:00",
                            "completed_at": "2026-10-05T07:00:00+00:00"},
             "batch": {"status": "succeeded", "result_published": True}}
    checkpoint = {"files": [], "state": {"generation": 80, "commands": [
        {"command": ["gmx", "mdrun"], "exit_code": 0, "wall_seconds": 23000,
         "checkpoint_step": 43963440, "performance_ns_per_day": 250}]}}
    result = delivery_gate(fixture, 13963440, final, checkpoint)
    assert result["newly_completed_ns"] == 60
    assert result["delivered_ns_per_day"] == pytest.approx(205.7142857)
    assert result["status"] == "passed"
    too_short = copy.deepcopy(checkpoint)
    too_short["state"]["commands"][0]["wall_seconds"] = 20000
    assert delivery_gate(fixture, 13963440, final, too_short)["status"] == "failed"
    final["operation"]["completed_at"] = "2026-10-05T08:00:00+00:00"
    assert delivery_gate(fixture, 13963440, final, checkpoint)["status"] == "failed"


def test_runner_owner_check_never_accepts_customer_key():
    spec = importlib.util.spec_from_file_location("demo_acceptance_runner", HERE / "run_demo_resume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    policy = {"tenant_id": "demo-user", "principal_id": "demo-user", "models": ["gromacs"], "max_concurrency": 2}
    module.validate_owner(policy)
    for changes in ({"tenant_id": "lynx"}, {"principal_id": "lynx"}, {"max_concurrency": 8}):
        with pytest.raises(ValueError):
            module.validate_owner({**policy, **changes})


@pytest.mark.parametrize("damage", [None, "bytes", "missing", "duplicate", "foreign", "generation"])
def test_independent_customer_object_bytes_and_generation(damage):
    import io
    data = b"closed native trajectory"
    digest = hashlib.sha256(data).hexdigest()
    file = {"path": "part.xtc", "sha256": digest, "size_bytes": len(data)}
    checkpoint = {"state": {"operation_id": "demo-operation", "generation": 4}, "files": [file],
                  "customer_storage": {"bucket": "demo-bucket", "prefix": "runs/demo", "manifest_key": "runs/demo/checkpoint.json"}}
    manifest = {"state": dict(checkpoint["state"]), "bucket": "demo-bucket",
                "files": [{**file, "key": "runs/demo/objects/" + digest}]}
    if damage == "missing":
        manifest["files"] = []
    elif damage == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif damage == "foreign":
        manifest["files"][0]["key"] = "runs/other-owner/objects/" + digest
    elif damage == "generation":
        manifest["state"]["generation"] = 3

    class Storage:
        def get_object(self, *, Bucket, Key):
            assert Bucket == "demo-bucket"
            if Key == "runs/demo/checkpoint.json":
                content = json.dumps(manifest).encode()
            else:
                assert Key == "runs/demo/objects/" + digest
                content = (b"x" * len(data)) if damage == "bytes" else data
            return {"Body": io.BytesIO(content), "ContentLength": len(content)}

    if damage is not None:
        with pytest.raises(ValueError):
            verify_customer_storage(Storage(), checkpoint, expected_bucket="demo-bucket")
    else:
        result = verify_customer_storage(Storage(), checkpoint, expected_bucket="demo-bucket")
        assert result["status"] == "passed"
        assert result["unique_objects"] == 1
        assert result["unique_bytes"] == len(data)
