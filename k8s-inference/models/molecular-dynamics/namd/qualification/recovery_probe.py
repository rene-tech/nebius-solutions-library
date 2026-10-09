"""Stage/restore a native checkpoint across task-owned Pod replacement.

The staging process uses a local archive as the acknowledged durable medium.
This tests native files on a fresh worker, not cloud transport or GPU snapshots.
The caller copies the archive out before deleting the old task-owned Pod.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

from fs2_gromacs.files import atomic_json, digest_file, extract_inputs
from fs2_namd.worker import Workflow, colvars_step, xsc_step


def verify_files(data, files):
    for item in files:
        path = data / item["path"]
        if path.stat().st_size != item["size_bytes"] or digest_file(path) != item["sha256"]:
            raise ValueError("native checkpoint archive failed its file manifest")


def stage(args):
    args.workspace.mkdir(parents=True, exist_ok=False)
    args.snapshot.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.fixture / "input.tar.gz", args.workspace / "input.tar.gz")
    atomic_json(args.workspace / ".fs2/restore-complete.json", {"status": "ready"})
    request = json.loads((args.fixture / "request.json").read_text())
    job = request["jobs"][0]["id"]
    with (args.workspace / "worker.log").open("w") as output:
        child = subprocess.Popen([sys.executable, "-m", "fs2_namd.worker", "--request", str(args.fixture / "request.json"),
                                  "--job-id", job, "--operation-id", args.operation, "--workspace", str(args.workspace),
                                  "--checkpoint-mode", "companion"], stdout=output, stderr=subprocess.STDOUT)
    ready = args.workspace / ".fs2/checkpoint-ready.json"
    last = 0
    deadline = time.monotonic() + 1800
    receipt = None
    while time.monotonic() < deadline and child.poll() is None:
        if ready.exists():
            manifest = json.loads(ready.read_text())
            state = manifest["state"]
            generation = state["generation"]
            if generation > last:
                last = generation
                active = state["active_step"]
                if active and active["id"] == "production":
                    verify_files(args.workspace / "data", manifest["files"])
                    atomic_json(args.snapshot / "manifest.json", manifest)
                    shutil.copyfile(args.fixture / "request.json", args.snapshot / "request.json")
                    archive = args.snapshot / "checkpoint.tar.gz"
                    with tarfile.open(archive, "w:gz") as bundle:
                        for item in manifest["files"]:
                            bundle.add(args.workspace / "data" / item["path"], arcname=item["path"], recursive=False)
                    restart = active["restart"]
                    directory = next(s["directory"] for s in request["jobs"][0]["steps"] if s["id"] == "production")
                    native = args.workspace / "data" / directory
                    receipt = {"operation_id": args.operation, "job_id": job, "generation": generation,
                               "checkpoint_step": xsc_step(native / restart["cell"]),
                               "colvars_step": colvars_step(native / restart["colvars_state"]) if "colvars_state" in restart else None,
                               "archive_sha256": digest_file(archive), "manifest_sha256": digest_file(args.snapshot / "manifest.json"),
                               "medium": "local archive copied out of old Pod by operator", "gpu_snapshot_used": False}
                    atomic_json(args.snapshot / "staged.json", receipt)
                atomic_json(args.workspace / ".fs2/checkpoint-ack.json", {"generation": generation, "status": "committed"})
                if receipt:
                    break
        time.sleep(0.2)
    if receipt is None:
        raise RuntimeError("native worker did not stage a production checkpoint; preserve its logs")
    # Observe real post-checkpoint work before allowing the caller to kill Pod.
    continued = args.workspace / "data" / directory / "fs2-production-part000002.log"
    while time.monotonic() < deadline and child.poll() is None:
        if continued.exists() and "ENERGY:" in continued.read_text():
            receipt["post_checkpoint_native_process_running"] = True
            receipt["observed_at_unix"] = time.time()
            atomic_json(args.snapshot / "staged.json", receipt)
            print(json.dumps({"ready_for_task_owned_pod_replacement": receipt}), flush=True)
            return
        time.sleep(0.2)
    raise RuntimeError("post-checkpoint native process was not observed")


def restore(args):
    staged = json.loads((args.snapshot / "staged.json").read_text())
    manifest = json.loads((args.snapshot / "manifest.json").read_text())
    archive = args.snapshot / "checkpoint.tar.gz"
    if digest_file(archive) != staged["archive_sha256"] or digest_file(args.snapshot / "manifest.json") != staged["manifest_sha256"]:
        raise ValueError("transferred native checkpoint archive identity changed")
    args.workspace.mkdir(parents=True, exist_ok=False)
    extract_inputs(archive, args.workspace / "data", max_bytes=8 * 1024**3)
    verify_files(args.workspace / "data", manifest["files"])
    # Platform metadata travels in the separately hashed manifest, never in the
    # scientist-input extractor's reserved .fs2 namespace.
    atomic_json(args.workspace / ".fs2/namd-state.json", manifest["state"])
    request = json.loads((args.snapshot / "request.json").read_text())
    result = Workflow(request, job_id=staged["job_id"], operation_id=staged["operation_id"],
                      workspace=args.workspace, checkpoint_mode="local").run()
    receipt = {"status": result["status"], "error": result["error"], "restored_generation": staged["generation"],
               "restored_step": staged["checkpoint_step"], "restored_colvars_step": staged["colvars_step"],
               "final_generation": result["native_checkpoint_generation"], "recipe_sha256": result["recipe_sha256"],
               "result_sha256": digest_file(args.workspace / "result.json"), "gpu_snapshot_used": False,
               "commands_before": len(manifest["state"]["commands"]), "commands_after": len(result["commands"])}
    atomic_json(args.workspace / "recovery-receipt.json", receipt)
    print(json.dumps(receipt), flush=True)
    if result["status"] != "succeeded":
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "restore"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--operation", default="namd-colvars-fresh-worker-r3")
    args = parser.parse_args()
    (stage if args.action == "stage" else restore)(args)


if __name__ == "__main__":
    main()
