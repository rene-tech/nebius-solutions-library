#!/usr/bin/env python3
"""Verify fresh-pod restore, GPU-allocation release and variable scientific inputs.

Run only after process_checkpoint.py capture has succeeded. Each repetition
deletes the donor/previous restore pod, creates a new GPU pod, waits for HTTP
readiness, verifies model bytes, and submits two full-production fold requests.
The same-node disk page cache is intentionally retained and reported honestly.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import time

SEQUENCES = (
    "MKTVRQERLKSIVRILERSKEPVSGAQLAEELSVSRQVIVQDIAYLRSLGYNIVATPRGYVLAGG",
    "MKTIIALSYIFCLVFADYKDDDDKGGGGSGGGGS",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--donor", required=True)
    parser.add_argument("--donor-already-deleted", action="store_true", help="Resume after verified prior donor deletion")
    parser.add_argument("--restore-template", type=Path, required=True)
    parser.add_argument("--baseline-state", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--eviction-holder", help="CPU-only pod holding the checkpoint volume")
    parser.add_argument("--checkpoint-directory", help="Exact images directory for file-scoped cache eviction")
    args = parser.parse_args()
    template = json.loads(args.restore_template.read_text())
    baseline = json.loads(args.baseline_state.read_text())
    namespace = template["metadata"]["namespace"]
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", namespace]
    args.output_directory.mkdir(parents=True, exist_ok=True)

    def run(arguments: list[str], *, payload: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*kubectl, *arguments], input=payload, text=True, capture_output=True,
            timeout=600, check=check,
        )

    def request(pod: str, path: str, body: dict | None = None) -> dict:
        code = (
            "import json,urllib.request; "
            f"body={body!r}; "
            f"request=urllib.request.Request('http://127.0.0.1:8000{path}', "
            "data=None if body is None else json.dumps(body).encode(), "
            "headers={'Content-Type':'application/json'}); "
            "print(urllib.request.urlopen(request,timeout=300).read().decode())"
        )
        result = run(["exec", pod, "-c", "runtime", "--", "/opt/esm/.pixi/envs/gpu/bin/python", "-c", code])
        return json.loads(result.stdout)

    receipt = {
        "schema": "fs2-serve.nebius.ai/esmfold2-persisted-pod-restore-probe/v1",
        "production_enabled": False,
        "cache_state": "same-node Network SSD checkpoint; operating-system page cache retained",
        "baseline": baseline, "runs": [], "status": "running",
    }
    if args.eviction_holder:
        if not args.checkpoint_directory:
            parser.error("file-scoped eviction requires --checkpoint-directory")
        receipt["cache_state"] = "persisted Network SSD; checkpoint files fadvise DONTNEED after prior GPU pod deletion"
    active_pod = args.donor
    try:
        donor = run(["get", "pod", active_pod, "--ignore-not-found", "-o", "json"]).stdout
        if donor:
            receipt["donor"] = json.loads(donor)["metadata"]["uid"]
        elif args.donor_already_deleted:
            receipt["donor"] = {"name": active_pod, "already_deleted": True}
        else:
            raise RuntimeError("donor is absent; use --donor-already-deleted only when resuming a prior deletion")
        for repetition in range(1, args.repetitions + 1):
            previous_pod = active_pod
            run(["delete", "pod", previous_pod, "--ignore-not-found=true", "--wait=true", "--timeout=90s"])
            deleted = run(["get", "pod", previous_pod, "--ignore-not-found", "-o", "name"]).stdout.strip() == ""
            if not deleted:
                raise RuntimeError("previous GPU pod still exists")
            pod = copy.deepcopy(template)
            active_pod = f"fs2-esmfold2-persist-restore-r{repetition}-20260906"
            pod["metadata"]["name"] = active_pod
            row = {"repetition": repetition, "previous_pod_deleted": deleted, "pod": active_pod}
            receipt["runs"].append(row)
            if args.eviction_holder:
                eviction = (
                    "import json,os,pathlib,sys; files=[]; "
                    "root=pathlib.Path(sys.argv[1]); "
                    "assert root.name=='images' and root.is_absolute(); "
                    "\nfor path in root.iterdir():\n"
                    " if path.is_file() and path.suffix=='.img':\n"
                    "  with path.open('rb') as stream:\n"
                    "   os.fsync(stream.fileno()); os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_DONTNEED)\n"
                    "  files.append({'name':path.name,'bytes':path.stat().st_size})\n"
                    "print(json.dumps({'operation':'file-scoped-fadvise-DONTNEED','files':files}))"
                )
                row["cache_eviction"] = json.loads(run([
                    "exec", args.eviction_holder, "--", "/opt/esm/.pixi/envs/gpu/bin/python",
                    "-c", eviction, args.checkpoint_directory,
                ]).stdout)
            started = time.monotonic()
            run(["apply", "-f", "-"], payload=json.dumps(pod))
            while time.monotonic() - started < 480:
                observed = json.loads(run(["get", "pod", active_pod, "-o", "json"]).stdout)
                if observed["status"]["phase"] == "Failed":
                    raise RuntimeError("restore pod failed before HTTP readiness")
                try:
                    row["ready"] = request(active_pod, "/health")
                    break
                except subprocess.CalledProcessError:
                    time.sleep(1)
            else:
                raise TimeoutError("restored worker did not become request-ready")
            row["pod_to_ready_seconds"] = time.monotonic() - started
            row["pod_uid"] = observed["metadata"]["uid"]
            row["state"] = request(active_pod, "/model-state")
            if row["state"] != baseline["state"]:
                raise RuntimeError("restored model tensor bytes differ")
            row["results"] = []
            for index, sequence in enumerate(SEQUENCES):
                result = request(active_pod, "/fold", {
                    "sequence": sequence, "num_loops": 20, "num_sampling_steps": 200, "seed": 42,
                })
                output = args.output_directory / f"restore-{repetition}-{index}.cif"
                output.write_text(result.pop("cif"))
                result["output"] = output.name
                row["results"].append(result)
            logs = run(["logs", active_pod, "-c", "runtime"]).stdout
            (args.output_directory / f"restore-{repetition}.log").write_text(logs)
            row["restore_lifecycle"] = json.loads(logs)
            row["status"] = "passed"
            (args.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            receipt["command_stderr"] = error.stderr
        failure_logs = run(["logs", active_pod, "-c", "runtime"], check=False)
        (args.output_directory / "failure.log").write_text(failure_logs.stdout + failure_logs.stderr)
    finally:
        (args.output_directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
