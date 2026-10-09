import csv
import json

import pytest

import restart_check
from screen import sha
from summarize_profile import summarize


def restart_fixture(tmp_path, *, bad_energy=None):
    records = []
    for variant in ("baseline-triclinic", "orthogonal"):
        case = tmp_path / variant
        case.mkdir()
        atoms = "ITEM: ATOMS id type x y z fx fy fz\n" + "".join(f"{i} 1 1 2 3 4 5 6\n" for i in range(1, 6599))
        (case / "initial-forces.lammpstrj").write_text(atoms)
        (case / "initial-energy.txt").write_text(" ".join([bad_energy if variant == "orthogonal" and bad_energy else "1"] * 9))
        records.append({"variant": variant, "exit_code": 0, "files": {p.name: sha(p) for p in case.iterdir()}})
    (tmp_path / "receipt.json").write_text(json.dumps({"status": "native-complete", "scope": "synthetic unit fixture", "results": records}))


def test_restart_validation_checks_all_atoms_and_no_overwrite(tmp_path):
    restart_fixture(tmp_path)
    result = restart_check.validate(tmp_path)
    assert result["atoms"] == 6598 and result["max_force_delta_kcal_mol_A"] == 0
    with pytest.raises(FileExistsError):
        restart_check.validate(tmp_path)


@pytest.mark.parametrize("energy", ["nan", "2"])
def test_restart_validation_rejects_nonfinite_or_changed_physics(tmp_path, energy):
    restart_fixture(tmp_path, bad_energy=energy)
    with pytest.raises(ValueError):
        restart_check.validate(tmp_path)


def test_restart_validation_rejects_changed_inventory(tmp_path):
    restart_fixture(tmp_path)
    (tmp_path / "orthogonal/initial-energy.txt").write_text("2 " * 9)
    with pytest.raises(ValueError, match="identity"):
        restart_check.validate(tmp_path)


def test_profile_summarizer_binds_raw_evidence_and_excludes_throughput(tmp_path):
    for variant in ("baseline-triclinic", "orthogonal"):
        case = tmp_path / (variant + "-r1")
        case.mkdir()
        for name in ("timeline.qdstrm", "timeline.nsys-rep", "timeline.sqlite", "summary_cuda_api_sum.csv", "native.log", "measurement.json"):
            (case / name).write_text("synthetic fixture")
        with (case / "summary_cuda_gpu_kern_sum.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=("Name", "Total Time (ns)", "Instances", "Avg (ns)"))
            writer.writeheader()
            for name, total in (("TagPPPM_compute_gf_ik", 1000), ("kiss_fft_functor", 3000)):
                writer.writerow({"Name": name, "Total Time (ns)": total, "Instances": 10, "Avg (ns)": total / 10})
    validation = tmp_path / "scientific-validation.json"
    validation.write_text(json.dumps({"status": "passed", "unprofiled_summary": {}}))
    report = summarize(tmp_path)
    assert report["timings_are_not_production_performance"] is True
    assert report["variants"]["orthogonal"]["selected_kernels"][0]["summed_kernel_time_percent"] == 25
    assert report["scientific_validation_sha256"] == sha(validation)
    validation.write_text(json.dumps({"status": "passed", "unprofiled_summary": {"bogus_speed": 100}}))
    with pytest.raises(ValueError, match="throughput"):
        summarize(tmp_path)
