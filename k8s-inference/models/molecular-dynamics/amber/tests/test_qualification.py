import importlib.util
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "qualification/ti_validation.py"
spec = importlib.util.spec_from_file_location("ti_validation", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_all_ti_states_and_scheduled_mbar_blocks_are_required():
    mdin = "ifmbar=1, mbar_states=11, bar_intervall=100, ntpr=100, clambda=0.30,\nmbar_lambda=0.0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,\n"
    block = "MBAR Energy analysis:\n" + "\n".join(f"Energy at {i / 10:.4f} = {-100 + i:.8f}" for i in range(11))
    output = "DV/DL = 12.4\n" + block + "\n" + block
    result = module.validate_ti(mdin, output, 200)
    assert result["mbar_energy_blocks"] == 2
    assert result["mbar_energy_values"] == 22
    assert result["free_energy_convergence_claimed"] is False
    assert module.validate_ti(mdin.replace("ntpr=100,", "ntpr=1000,"), output, 2000)["mbar_print_interval_steps"] == 1000
    with pytest.raises(ValueError, match="scheduled"):
        module.validate_ti(mdin, block + "\nDV/DL = 12.4", 200)
    with pytest.raises(ValueError, match="coverage"):
        module.validate_ti(mdin, output.replace("Energy at 1.0000 = -90.00000000", ""), 200)
    with pytest.raises(ValueError, match="nonfinite"):
        module.validate_ti(mdin, output.replace("DV/DL = 12.4", "DV/DL = NaN"), 200)


def load_qualifier(name):
    path = Path(__file__).parents[1] / ("qualification/" + name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target


def test_static_native_fields_distinguish_total_and_one_four_electrostatics():
    static = load_qualifier("static_energy")
    text = "BOND=0.1264 ANGLE=0.3620 DIHED=9.6440\nVDWAALS=2209.5285 EEL=-20700.5546 HBOND=0.0000\n1-4 VDW=5.0157 1-4 EEL=48.9355 RESTRAINT=0.0000\n"
    terms = static.energy_terms(text)
    assert len(terms) == 9
    assert terms["EEL"] == -20700.5546
    assert terms["1-4 EEL"] == 48.9355
    assert sum(terms.values()) == pytest.approx(-18426.9425)


def test_canonical_fixture_keeps_master_physics_and_real_pressure_method(tmp_path):
    generator = load_qualifier("make_canonical_fixture")
    master = tmp_path / "master"
    master.mkdir()
    protocol = {"molecule": "ACE-ALA-NME", "force_field": "Amber ff14SB", "water_model": "TIP3P", "temperature_K": 300.0, "pressure_bar": 1.0, "timestep_fs": 2.0, "cutoff_A": 10.0, "langevin_friction_per_ps": 1.0, "nvt_steps": 50000, "npt_steps": 50000, "production_steps": 500000, "output_every_steps": 500, "constraint_tolerance": 1e-6, "nvt_seed": 20260923, "npt_seed": 20260924, "production_seed": 20260925}
    (master / "protocol.json").write_text(json.dumps(protocol))
    for name in ("system.prmtop", "system.rst7", "system.pdb", "master-manifest.json"):
        (master / name).write_bytes(b"immutable synthetic test bytes\n")
    output = tmp_path / "fixture"
    generator.make(master, output)
    with tarfile.open(output / "input.tar.gz") as archive:
        assert archive.extractfile("system.prmtop").read() == (master / "system.prmtop").read_bytes()
        minimum = archive.extractfile("minimize.in").read().decode()
        production = archive.extractfile("production-001.in").read().decode()
        assert "ntc=1, ntf=1" in minimum
        for setting in ("nstlim=500000", "barostat=1, baro_stochastic=1", "ischeme=1, ithermostat=1, therm_par=1.0", "ntwv=500", "ig=20260925", "vdwmeth=1", "tol=0.000001", "skinnb=2.0, skin_permit=0.5"):
            assert setting in production
    protocol["water_model"] = "OPC"
    (master / "protocol.json").write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="canonical master"):
        generator.make(master, tmp_path / "invalid")


def test_binding_arithmetic_covers_both_models_and_all_components(tmp_path):
    validator = load_qualifier("validate_advanced_fixture")
    rows = []
    values = {"Complex": "2,5,5,10", "Receptor": "0.5,1,2,3", "Ligand": "0.5,1,1,2", "DELTA": "1,3,2,5"}
    for model in ("GENERALIZED BORN:", "POISSON BOLTZMANN:"):
        rows.append(model)
        for component, numbers in values.items():
            rows.extend([component + " Energy Terms", "Frame #,BOND,G gas,G solv,TOTAL"])
            rows.extend(str(frame) + "," + numbers for frame in range(5))
    path = tmp_path / "binding.csv"
    path.write_text("\n".join(rows) + "\n")
    assert len(validator.binding_arithmetic(path)) == 2
    path.write_text(path.read_text().replace("0,1,3,2,5", "0,1,3,2,6", 1))
    with pytest.raises(ValueError, match="arithmetic"):
        validator.binding_arithmetic(path)


def test_upstream_summary_retains_failure_and_verifies_copied_files(tmp_path):
    module = load_qualifier("regression_receipt")
    log = tmp_path / "native.log"
    log.write_bytes(b"upstream comparison retained\n")
    sha = hashlib.sha256(log.read_bytes()).hexdigest()
    tests = [{"case": "case-" + str(i // 2), "precision": "SPFP" if i % 2 else "DPFP", "status": "failed" if i == 1 else "passed", "argv": ["native-upstream"], "exit_code": 0, "timed_out": False, "wall_seconds": 1, "upstream_script_sha256": "a" * 64, "log": "native.log", "log_sha256": sha, "source_files": []} for i in range(14)]
    receipt = {"status": "failed", "files": [{"path": "native.log", "size_bytes": log.stat().st_size, "sha256": sha}], "tests": tests, "gpu": "NVIDIA L40S, GPU-test, 580.173.02, 8.9", "engine_id": "private@sha256:" + "a" * 64, "pmemd_source_sha256": "b" * 64, "comparison_policy": "unchanged upstream"}
    (tmp_path / "regression.json").write_text(json.dumps(receipt))
    pod = {"spec": {"containers": [{"image": "private@sha256:" + "c" * 64}], "nodeName": "synthetic"}, "status": {"containerStatuses": [{"imageID": "private@sha256:" + "c" * 64}]}, "metadata": {"uid": "synthetic"}}
    result = module.summarize(tmp_path, pod, "d" * 40)
    assert result["status"] == "failed"
    assert result["passed_comparisons"] == 13
    assert result["pool"] == "l40s-1x"
    log.write_bytes(b"changed log\n")
    with pytest.raises(ValueError, match="mismatch"):
        module.summarize(tmp_path, pod, "d" * 40)
