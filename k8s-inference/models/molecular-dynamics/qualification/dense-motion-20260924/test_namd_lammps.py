import importlib.util
import json
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("dense_native", Path(__file__).with_name("namd_lammps.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def synthetic_delivery(tmp_path, engine):
    root = tmp_path / "delivery"
    root.mkdir()
    files = {"case.json": json.dumps({"engines": {engine: {"worker_image": m.WORKERS[engine]}}}),
             f"runs/{engine}/result.json": json.dumps({"status": "succeeded", "operation_id": "synthetic",
                "completed_steps": ["minimize", "nvt", "npt", "production"], "commands": [{"native_restart_step": 600000}]})}
    prefix = f"runs/{engine}/data/" + ("alanine/" if engine == "namd" else "")
    files.update({prefix + name: "synthetic\n" for name in ("protocol.json", "master-manifest.json")})
    if engine == "namd":
        files.update({prefix + name: "synthetic\n" for name in ("system.prmtop", "system.rst7", "system.pdb", "production.coor", "production.vel")})
        files[prefix + "production.xsc"] = "# cell\n605000 40 0 0 0 40 0 0 0 40 0 0 0\n"
        files[prefix + "production.namd"] = "seed 20260925\ntimestep 2.0\nPMEGridSizeX 64\n" + "".join(f"{v} 500\n" for v in ("outputEnergies", "outputPressure", "DCDfreq", "XSTfreq"))
    else:
        files.update({prefix + name: "synthetic\n" for name in ("system-shake.lmp", "adapter-manifest.json", "restart-coefficients.inc", "nonbonded.inc", "constraints.inc", "production.restart")})
        files[prefix + "production-progress.txt"] = "600000\n"
        files[prefix + "thermo.inc"] = "thermo 500\n"
        files[prefix + "production-resume.in"] = "read_restart production.restart\ndump coordinates all custom 500 production.${fs2_segment}.lammpstrj id type x y z vx vy vz\nrun 600000 upto\nwrite_restart production.restart\nprint step file production-progress.txt\n"
        files[prefix + "production.2.lammpstrj"] = "ITEM: TIMESTEP\n600000\nITEM: NUMBER OF ATOMS\n2\nITEM: BOX BOUNDS pp pp pp\n0 40\n0 40\n0 40\nITEM: ATOMS id type x y z vx vy vz\n1 1 0 0 0 0 0 0\n2 1 1 1 1 0 0 0\n"
    inventory = []
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        inventory.append({"path": name, "bytes": path.stat().st_size, "sha256": m.sha(path)})
    m.save(root / "delivery-manifest.json", {"files": inventory})
    return root


@pytest.mark.parametrize("engine,start,end", [("namd", 605000, 615000), ("lammps", 600000, 610000)])
def test_native_origins_and_frozen_input(tmp_path, monkeypatch, engine, start, end):
    monkeypatch.setattr(m, "ATOMS", 2)
    delivery = synthetic_delivery(tmp_path, engine)
    before = {str(p.relative_to(delivery)): m.sha(p) for p in delivery.rglob("*") if p.is_file()}
    output = tmp_path / "fixture"
    fixture = m.prepare(delivery, output, engine)
    assert (fixture["start_step"], fixture["final_step"], fixture["steps"], fixture["nonzero_frames"]) == (start, end, 10000, 1000)
    assert before == {str(p.relative_to(delivery)): m.sha(p) for p in delivery.rglob("*") if p.is_file()}
    request = m.read(output / "request.json")
    assert len(request["jobs"]) == len(request["jobs"][0]["steps"]) == 1
    assert request["output_destination"] == "platform-artifacts"
    with tarfile.open(output / "input.tar.gz") as archive:
        assert all(not p.name.startswith("/") and ".." not in Path(p.name).parts and p.mtime == 0 for p in archive)
    if engine == "namd":
        native = (output / "inputs/alanine/dense.namd").read_text()
        assert "seed 20260925" in native and "DCDfreq 10\n" in native and "timestep 2.0" in native
        assert request["jobs"][0]["steps"][0]["restart"]["cell"] == "origin.xsc"
    else:
        native = (output / "inputs/dense.in").read_text()
        assert "read_restart origin.restart" in native and "run 610000 upto" in native
        assert "custom 10 dense.${fs2_segment}" in native


def test_wrong_checkpoint_step_rejected(tmp_path):
    root = synthetic_delivery(tmp_path, "namd")
    path = root / "runs/namd/data/alanine/production.xsc"
    path.write_text("600000 40 0 0 0 40 0 0 0 40 0 0 0\n")
    inventory = m.read(root / "delivery-manifest.json")
    row = next(r for r in inventory["files"] if r["path"].endswith("production.xsc"))
    row.update(sha256=m.sha(path), bytes=path.stat().st_size)
    (root / "delivery-manifest.json").write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="XSC timestep"):
        m.prepare(root, tmp_path / "fixture", "namd")


def test_reject_changed_frozen_source(tmp_path):
    root = synthetic_delivery(tmp_path, "namd")
    (root / "runs/namd/data/alanine/production.vel").write_text("changed")
    with pytest.raises(ValueError, match="inventory mismatch"):
        m.prepare(root, tmp_path / "fixture", "namd")


def test_no_original_output_directory(tmp_path):
    root = synthetic_delivery(tmp_path, "namd")
    with pytest.raises(ValueError, match="outside frozen"):
        m.prepare(root, root / "new", "namd")


@pytest.mark.parametrize("source", ["absent", "a a "])
def test_ambiguous_native_rewrite_rejected(source):
    with pytest.raises(ValueError, match="exactly one"):
        m.replace_once(source, "a ", "b ")


def test_reproducible_archive(tmp_path):
    source = tmp_path / "inputs"
    source.mkdir()
    (source / "data.in").write_text("native input\n")
    m.bundle(source, tmp_path / "a.tar.gz")
    m.bundle(source, tmp_path / "b.tar.gz")
    assert m.sha(tmp_path / "a.tar.gz") == m.sha(tmp_path / "b.tar.gz")


@pytest.mark.parametrize("engine", ["namd", "lammps"])
def test_published_digest_binding(engine):
    row = {"model_id": engine, "state": "active", "runtime_image_digest": m.WORKERS[engine].split("@")[1]}
    assert m.verify_runtime({"data": [row]}, engine, m.WORKERS[engine]) == row
    row["runtime_image_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="digest differs"):
        m.verify_runtime({"data": [row]}, engine, m.WORKERS[engine])
