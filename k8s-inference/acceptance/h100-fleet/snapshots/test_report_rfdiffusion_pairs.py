import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_rfdiffusion_pairs import original_result, restore_measurement


def archive(tmp_path, *, seed=8100, steps=50, residues=76):
    value = {
        "model_id": "rfdiffusion",
        "status": "succeeded",
        "accelerator": {"cuda_execution_confirmed": True},
        "designs": [
            {"residue_count": residues, "seed": seed, "pdb": {"sha256": "a" * 64}}
        ],
        "request": {
            "contigs": ["76-76"],
            "seed": 8100,
            "diffuser_T": steps,
            "num_designs": 1,
        },
        "upstream": {"model_ready_seconds": 24},
        "total_seconds": 45,
    }
    payload = json.dumps(value).encode()
    path = tmp_path / "outputs.tar"
    with tarfile.open(path, "w") as bundle:
        entry = tarfile.TarInfo("./result.json")
        entry.size = len(payload)
        bundle.addfile(entry, io.BytesIO(payload))
    return path


def test_original_native_result_retains_sampler_clock_and_input_identity(tmp_path):
    result = original_result(archive(tmp_path))
    assert result["native_sampler_ready_seconds"] == 24
    assert result["full_wrapper_seconds"] == 45
    assert result["residues"] == 76
    assert result["seed"] == 8100


@pytest.mark.parametrize("change", [{"seed": 8101}, {"residues": 75}, {"steps": 20}])
def test_changed_scientific_contract_cannot_qualify(tmp_path, change):
    with pytest.raises(ValueError):
        original_result(archive(tmp_path, **change))


def test_actual_criu_and_cuda_timings_are_required_not_selected_policy(tmp_path):
    value = {"action": "restore", "status": "passed", "records": [
        {"command": ["criu", "restore"], "seconds": 0.7, "returncode": 0},
        {"command": ["cuda-checkpoint", "--action", "restore"], "seconds": 0.2, "returncode": 0},
        {"command": ["cuda-checkpoint", "--action", "unlock"], "seconds": 0.02, "returncode": 0},
    ]}
    text = json.dumps(value, indent=2) + '\n{"mechanism": "cuda-criu-restored"}'
    path = tmp_path / "lifecycle.log"
    path.write_text("\n".join("2026-09-07T12:00:00Z " + line for line in text.splitlines()))
    result = restore_measurement(path)
    assert result["criu_seconds"] == 0.7
    assert result["cuda_restore_seconds"] == 0.2
    assert result["cuda_unlock_seconds"] == 0.02
    path.write_text(path.read_text().replace("cuda-criu-restored", "normal-load-fallback"))
    with pytest.raises(ValueError, match="confirm"):
        restore_measurement(path)
