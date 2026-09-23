"""Terminate an active native worker; restore a committed workspace in a new PID.

This emulates the file acknowledgement protocol without remote credentials. It
proves native restart recovery only, not tenant transport or a GPU process image.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from fs2_gromacs.files import atomic_json, digest_file


def command(root, job):
    return ["python3", "-m", "fs2_lammps.worker", "--request", str(root / "request.json"), "--workspace", str(root), "--job-id", job, "--operation-id", "da20cd30-3205-4cd2-a99e-63943fee0923", "--checkpoint-mode", "companion"]


def drive(root, job, *, interrupt=False, snapshot=None):
    meta = root / ".fs2"
    meta.mkdir(exist_ok=True)
    atomic_json(meta / "restore-complete.json", {"status": "ready"})
    generations, last = [], 0
    with (root / "driver-worker.log").open("w") as output:
        process = subprocess.Popen(command(root, job), stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 600
        interrupted_at = None
        while process.poll() is None and time.monotonic() < deadline:
            ready = meta / "checkpoint-ready.json"
            if ready.is_file():
                value = json.loads(ready.read_text())
                state = value["state"]
                if state["generation"] > last:
                    for entry in value["files"]:
                        path = root / "data" / entry["path"]
                        if path.stat().st_size != entry["size_bytes"] or digest_file(path) != entry["sha256"]:
                            process.terminate()
                            raise ValueError("file changed before checkpoint acknowledgement")
                    last = state["generation"]
                    generations.append({"generation": last, "active_step": state["active_step"], "completed_steps": state["completed_steps"]})
                    capture = interrupt and state["active_step"] is not None
                    if capture:
                        snapshot.mkdir(parents=True, exist_ok=False)
                        shutil.copytree(root / "data", snapshot / "data")
                        (snapshot / ".fs2").mkdir()
                        shutil.copy2(root / "request.json", snapshot / "request.json")
                        atomic_json(snapshot / ".fs2/lammps-state.json", state)
                    atomic_json(meta / "checkpoint-ack.json", {"status": "committed", "generation": last})
                    if capture:
                        # Wait until the next engine process is alive, after the
                        # generation was frozen and acknowledged.
                        for _ in range(100):
                            children_path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                            children = children_path.read_text().split() if children_path.exists() else []
                            if children:
                                break
                            time.sleep(0.05)
                        time.sleep(0.5)
                        process.send_signal(signal.SIGTERM)
                        interrupted_at = {"generation": last, "native_step": state["active_step"]["progress"], "worker_pid": process.pid, "native_child_pids": children}
                        interrupt = False
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
            process.wait()
            raise TimeoutError("qualification worker exceeded 600 seconds")
        code = process.wait()
    result = json.loads((root / "result.json").read_text())
    return {"worker_pid": process.pid, "exit_code": code, "status": result["status"], "completed_steps": result["completed_steps"], "commands": result["commands"], "generations": generations, "interrupted_at": interrupted_at}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    attempt = args.output / "interrupted"
    shutil.copytree(args.input, attempt)
    restored = args.output / "restored"
    first = drive(attempt, args.job, interrupt=True, snapshot=restored)
    if first["status"] != "interrupted" or first["exit_code"] != 143 or not first["interrupted_at"]:
        raise ValueError("attempt did not interrupt active committed native dynamics")
    second = drive(restored, args.job)
    expected = json.loads((restored / "request.json").read_text())["jobs"][0]["steps"]
    if second["status"] != "succeeded" or second["completed_steps"] != [s["id"] for s in expected]:
        raise ValueError("restored native worker did not complete")
    if first["worker_pid"] == second["worker_pid"]:
        raise ValueError("resume did not use a fresh worker process")
    receipt = {"interrupted": first, "restored": second, "native_recovery_passed": True, "gpu_process_snapshot": False, "fresh_pod": False, "customer_transport_tested": False, "snapshot_kind": "closed complete workspace plus native restart and continuation script"}
    (args.output / "interrupt-resume.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"native_recovery_passed": True, "checkpoint_step": first["interrupted_at"]["native_step"], "new_worker_pid": second["worker_pid"]}))


if __name__ == "__main__":
    main()
