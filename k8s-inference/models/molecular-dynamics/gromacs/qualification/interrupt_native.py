"""Interrupt only a test-owned worker after its first committed local segment.

Use a one-mdrun finite test job. Copy its workspace to a different task-owned
Pod afterwards and run the identical operation/job with the normal worker.
This tests native process restart, not remote artifact recovery or CUDA restore.
"""

import argparse
import json
import signal
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace
    root.mkdir(parents=True, exist_ok=False)
    (root / "data").mkdir()
    (root / "data/run.tpr").write_bytes(args.tpr.read_bytes())
    request = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1", "threads": 8,
        "segment_minutes": 0.1, "checkpoint_minutes": 0.1, "max_wall_seconds": 3600,
        "jobs": [{"id": "resume", "steps": [
            {"id": "production", "command": "mdrun", "args": ["-s", "run.tpr", "-deffnm", "md"]},
            {"id": "join", "command": "trjcat", "args": ["-f", {"files": "md.part*.xtc"}, "-o", "md.xtc"]},
            {"id": "check", "command": "check", "args": ["-f", "md.xtc"]},
        ]}]}
    (root / "request.json").write_text(json.dumps(request))
    with (root / "interruption.log").open("wb") as log:
        child = subprocess.Popen(["python3", "-m", "fs2_gromacs.worker", "--workspace", str(root),
            "--request", str(root / "request.json"), "--operation-id", "e6544ae5-e346-4993-9311-a63d1ae6b19e",
            "--job-id", "resume", "--checkpoint-mode", "local"], stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        marker = root / ".fs2/checkpoint-ready.json"
        signalled = False
        while time.monotonic() < deadline and child.poll() is None:
            if marker.is_file() and json.loads(marker.read_text())["state"]["generation"] >= 1:
                child.send_signal(signal.SIGTERM)
                signalled = True
                break
            time.sleep(0.05)
        if not signalled:
            child.terminate()
        code = child.wait(timeout=120)
    result = json.loads((root / "result.json").read_text())
    if not signalled or code != 143 or result["status"] != "interrupted":
        raise RuntimeError("the test did not interrupt an active checkpointed simulation")
    print(json.dumps({"signalled": signalled, "exit_code": code,
                      "generation": result["native_checkpoint_generation"], "status": result["status"]}))


if __name__ == "__main__":
    main()
