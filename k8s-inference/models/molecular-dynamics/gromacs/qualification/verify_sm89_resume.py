"""Verify the retained interrupted/resumed small-system native-worker fixture.

Run inside the destination Pod after copying and resuming an interrupted
workspace. This tests native checkpoint continuity, not remote bucket recovery.
"""

import argparse
import json
import math
from pathlib import Path
import re
import subprocess

from qualify_sm89_features import digest, numeric_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace
    original = json.loads((root / "interrupted-result.json").read_text())
    result = json.loads((root / "result.json").read_text())
    assert original["status"] == "interrupted"
    assert result["status"] == "succeeded"
    for key in ("operation_id", "job_id", "recipe_sha256", "engine_id"):
        assert original[key] == result[key], key
    assert result["completed_steps"] == ["production", "join", "check"]
    assert result["native_checkpoint_generation"] > original["native_checkpoint_generation"]
    assert not result["gpu_snapshot_used"]
    for entry in result["files"]:
        path = root / "data" / entry["path"]
        assert path.stat().st_size == entry["size_bytes"] and digest(path) == entry["sha256"], path
    interrupted_steps = [command.get("checkpoint_step", 0) for command in original["commands"]]
    production = [command for command in result["commands"] if command["step_id"] == "production"]
    assert max(interrupted_steps) > 0 and max(interrupted_steps) < 200000
    assert production[-1]["checkpoint_step"] == 200000
    assert any("-cpi" in command["command"] for command in production)
    checks = [command for command in result["commands"] if command["step_id"] == "check"]
    assert checks and checks[-1]["exit_code"] == 0
    check_log = (root / "data" / checks[-1]["log"]).read_text()
    frames = re.findall(r"Last frame\s+(\d+)\s+time\s+([\d.]+)", check_log)
    assert frames and int(frames[-1][0]) == 200 and float(frames[-1][1]) == 400
    binary = production[-1]["command"][0]
    energies = []
    for path in sorted((root / "data").glob("md.part*.edr")):
        output = root / (path.stem + "-energy.xvg")
        with (root / (path.stem + "-energy.log")).open("wb") as log:
            subprocess.run(
                [binary, "energy", "-f", str(path), "-o", str(output)],
                input=b"Potential\nKinetic-En.\nTotal-Energy\nTemperature\nPressure\n0\n",
                stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60,
            )
        rows = numeric_rows(output)
        energies.append({"file": path.name, "rows": len(rows), "first_ps": rows[0][0], "last_ps": rows[-1][0]})
    assert energies and math.isclose(energies[0]["first_ps"], 0)
    assert math.isclose(energies[-1]["last_ps"], 400)
    assert all(a["last_ps"] <= b["first_ps"] for a, b in zip(energies, energies[1:]))
    receipt = {
        "status": "passed", "engine_id": result["engine_id"],
        "operation_id": result["operation_id"], "job_id": result["job_id"],
        "interrupted_checkpoint_step": max(interrupted_steps), "final_checkpoint_step": 200000,
        "original_generation": original["native_checkpoint_generation"],
        "final_generation": result["native_checkpoint_generation"],
        "trajectory_frames": 201, "final_time_ps": 400,
        "verified_files": len(result["files"]), "energies": energies,
        "customer_path_tested": False, "gpu_snapshot_used": False,
    }
    (root / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
