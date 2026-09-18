#!/usr/bin/env python3
"""Execute frozen real-request stage commands in one isolated candidate Pod."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import traceback


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    plan = json.loads(Path("/inputs/plan.json").read_text())
    root = Path("/workspace")
    save(root / "packages.json", {p: importlib.metadata.version(p) for p in
         ("torch", "triton", "cuequivariance", "cuequivariance-torch",
          "cuequivariance-ops-cu12", "cuequivariance-ops-torch-cu12", "rc-foundry")})
    # No CPU-host inference about extension availability: import the actual
    # accelerated modules after NVIDIA runtime has supplied libcuda here.
    import torch
    from cuequivariance_ops_torch.attention_pair_bias_torch import attention_pair_bias
    from cuequivariance_ops_torch.triangle_multiplicative_update import triangle_multiplicative_update
    assert callable(attention_pair_bias) and callable(triangle_multiplicative_update)
    assert torch.cuda.is_available()
    (root / "gpu.csv").write_text(subprocess.check_output(
        ["nvidia-smi", "--query-gpu=uuid,name,driver_version,memory.total", "--format=csv,noheader"], text=True))
    # Reuse exact checked-in generation verifier for Complexa/RF3. The AF2
    # flat-inventory generation is separately retained in the plan/mounts.
    import runtime_entrypoint as entrypoint
    generations = [entrypoint.verify_generation(Path("/opt/fs2/artifacts") / key, key)
                   for key in entrypoint.GENERATIONS]
    save(root / "generation-verification.json", generations)
    outcomes = []
    for case in plan["cases"]:
        folder = root / case["case_id"]
        folder.mkdir()
        bundle = Path("/inputs") / case["bundle_name"]
        assert hashlib.sha256(bundle.read_bytes()).hexdigest() == case["bundle_sha256"]
        with tarfile.open(bundle, "r:gz") as archive:
            archive.extractall(folder, filter="data")
        record = {"case_id": case["case_id"], "parameters": case["parameters"], "stages": []}
        for stage in case["stages"]:
            started = time.time()
            print("STAGE_START", case["case_id"], stage["stage_id"], flush=True)
            environment = dict(os.environ, **stage["environment"])
            with (folder / (stage["stage_id"] + ".log")).open("w") as log:
                result = subprocess.run(stage["argv"], cwd=folder, env=environment,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=3600)
            record["stages"].append({"stage_id": stage["stage_id"], "returncode": result.returncode,
                                     "started_at_epoch": started, "elapsed_seconds": time.time()-started})
            save(folder / "execution.json", record)
            print("STAGE_END", case["case_id"], stage["stage_id"], result.returncode, flush=True)
            if result.returncode:
                break
        record["completed_all_stages"] = len(record["stages"]) == 4 and all(
            s["returncode"] == 0 for s in record["stages"])
        outcomes.append(record)
        save(root / "summary.json", outcomes)
    (root / "COMPLETE").touch()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        (Path("/workspace") / "FAILED").touch()
    finally:
        # Bounded collection window; parent/worker deletes only this owned Pod.
        time.sleep(1800)
