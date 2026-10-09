"""Interrupt active PMEMD, preserving only the preceding acknowledged stage.

This local companion emulation is a native closed-workspace recovery test, not
object-storage transport or a persistent CUDA process snapshot.
"""

import argparse
import json
import shutil
import signal
import subprocess
import time
from pathlib import Path

from fs2_gromacs.files import atomic_json, digest_file

OPERATION = "a0620000-0923-4026-8000-000000000002"


def verify(root, manifest):
    for item in manifest["files"]:
        path = root / "data" / item["path"]
        if path.stat().st_size != item["size_bytes"] or digest_file(path) != item["sha256"]:
            raise ValueError("closed-stage workspace inventory mismatch")


def native_children(process):
    path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
    found = []
    for pid in path.read_text().split() if path.exists() else []:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except FileNotFoundError:
            continue
        if "/opt/amber26/bin/pmemd.cuda_" in command:
            found.append({"pid": int(pid), "command": command})
    return found


def drive(root, job, *, capture_after=None, snapshot=None):
    meta = root / ".fs2"
    meta.mkdir(exist_ok=True)
    atomic_json(meta / "restore-complete.json", {"status": "ready"})
    command = ["python3", "-m", "fs2_amber.worker", "--request", str(root / "request.json"), "--workspace", str(root), "--job-id", job, "--operation-id", OPERATION, "--checkpoint-mode", "companion"]
    generations, last, interrupted_at = [], 0, None
    with (root / "driver-worker.log").open("x") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 1800
            while process.poll() is None and time.monotonic() < deadline:
                ready = meta / "checkpoint-ready.json"
                if ready.is_file():
                    manifest = json.loads(ready.read_text())
                    state = manifest["state"]
                    if state["generation"] > last:
                        verify(root, manifest)
                        last = state["generation"]
                        generations.append({"generation": last, "completed_steps": state["completed_steps"]})
                        capture = capture_after is not None and state["completed_steps"] and state["completed_steps"][-1] == capture_after
                        if capture:
                            snapshot.mkdir(parents=True, exist_ok=False)
                            shutil.copytree(root / "data", snapshot / "data")
                            (snapshot / ".fs2").mkdir()
                            for name in ("request.json", "input.tar.gz", "fixture-manifest.json"):
                                shutil.copy2(root / name, snapshot / name)
                            atomic_json(snapshot / ".fs2/amber-state.json", state)
                            atomic_json(snapshot / ".fs2/closed-manifest.json", manifest)
                            verify(snapshot, manifest)
                        atomic_json(meta / "checkpoint-ack.json", {"status": "committed", "generation": last})
                        if capture:
                            children = []
                            for _ in range(200):
                                children = native_children(process)
                                if children:
                                    break
                                time.sleep(0.05)
                            if not children:
                                raise ValueError("no active PMEMD CUDA process after committed stage")
                            time.sleep(0.75)
                            if not native_children(process):
                                raise ValueError("next PMEMD stage ended before interruption")
                            process.send_signal(signal.SIGTERM)
                            interrupted_at = {"generation": last, "completed_steps": state["completed_steps"], "worker_pid": process.pid, "native_children": children}
                            capture_after = None
                time.sleep(0.05)
            if process.poll() is None:
                raise TimeoutError("recovery qualification exceeded1800seconds")
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    result = json.loads((root / "result.json").read_text())
    return {"worker_pid": process.pid, "exit_code": code, "status": result["status"], "completed_steps": result["completed_steps"], "commands": result["commands"], "generations": generations, "interrupted_at": interrupted_at}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("interrupt", "resume"), required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--capture-after", default="production-001")
    args = parser.parse_args()
    restored = args.output / "restored"
    if args.phase == "interrupt":
        args.output.mkdir(parents=True, exist_ok=False)
        attempt = args.output / "interrupted"
        shutil.copytree(args.input, attempt)
        first = drive(attempt, args.job, capture_after=args.capture_after, snapshot=restored)
        atomic_json(args.output / "interrupted-receipt.json", first)
        if first["status"] != "interrupted" or first["exit_code"] != 143 or not first["interrupted_at"]:
            raise ValueError("attempt did not interrupt active PMEMD after a committed stage")
        print(json.dumps({"status": "interrupted", "closed_completed_steps": first["interrupted_at"]["completed_steps"]}))
        return
    first = json.loads((args.output / "interrupted-receipt.json").read_text())
    if first["status"] != "interrupted" or first["exit_code"] != 143:
        raise ValueError("donor did not preserve explicit incomplete status")
    manifest = json.loads((restored / ".fs2/closed-manifest.json").read_text())
    verify(restored, manifest)
    second = drive(restored, args.job)
    request = json.loads((restored / "request.json").read_text())
    expected = next(job for job in request["jobs"] if job["id"] == args.job)["steps"]
    if second["status"] != "succeeded" or second["completed_steps"] != [step["id"] for step in expected]:
        raise ValueError("fresh native worker did not complete the entire frozen workflow")
    committed = manifest["state"]["commands"]
    if second["commands"][:len(committed)] != committed:
        raise ValueError("committed stages changed or were re-executed during recovery")
    if [item["step_id"] for item in second["commands"]] != [step["id"] for step in expected]:
        raise ValueError("restored final command history is not exactly one successful command per stage")
    atomic_json(args.output / "interrupt-resume.json", {"interrupted": first, "restored": second, "native_recovery_passed": True, "gpu_process_snapshot": False, "customer_transport_tested": False, "recovery_kind": "closed full workspace, native restart and explicit subsequent stages", "retry_semantics": "interrupted active stage rerun from preceding committed boundary; interrupted attempt remains separate evidence"})
    print(json.dumps({"native_recovery_passed": True, "closed_completed_steps": manifest["state"]["completed_steps"]}))


if __name__ == "__main__":
    main()
