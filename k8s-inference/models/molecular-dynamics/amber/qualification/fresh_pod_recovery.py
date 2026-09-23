"""Recover a verified closed AMBER stage after replacing the owned GPU Pod."""

import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cluster import KUBE, NS, create, owned


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024**2), b""):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--new-pod", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-after", default="production-001")
    args = parser.parse_args()
    old = owned(args.pod)
    if args.pod == args.new_pod:
        raise ValueError("replacement must have a distinct Pod identity")
    processes = subprocess.check_output(KUBE + NS + ["exec", args.pod, "--", "ps", "-eo", "args"], text=True)
    if "-m fs2_amber.worker" in processes or "/opt/amber26/bin/pmemd.cuda_" in processes:
        raise ValueError("owned original Pod has an active native workflow")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    save(args.output / "original-pod.json", old)
    request = json.loads((args.fixture / "request.json").read_text())
    if len(request["jobs"]) != 1:
        raise ValueError("recovery fixture must contain exactly one native job")
    job = request["jobs"][0]["id"]
    remote = "/mnt/fs2-scientific/" + args.output.name
    remote_fixture = remote + "-fixture"
    code = Path(__file__).parent
    subprocess.run(KUBE + NS + ["cp", "--no-preserve", str(args.fixture), args.pod + ":" + remote_fixture], check=True)
    subprocess.run(KUBE + NS + ["cp", "--no-preserve", str(code), args.pod + ":/mnt/fs2-scientific/recovery-qualification"], check=True)
    script = "/mnt/fs2-scientific/recovery-qualification/interrupt_resume.py"
    with (args.output / "interrupt-client.log").open("w") as log:
        subprocess.run(KUBE + NS + ["exec", args.pod, "--", "python3", script, "--phase", "interrupt", "--input", remote_fixture, "--output", remote, "--job", job, "--capture-after", args.capture_after], stdout=log, stderr=subprocess.STDOUT, check=True)
    donor = args.output / "before-pod-deletion"
    subprocess.run(KUBE + NS + ["cp", "--retries=3", args.pod + ":" + remote, str(donor)], check=True)
    manifest_path = donor / "restored/.fs2/closed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        path = donor / "restored/data" / item["path"]
        if path.stat().st_size != item["size_bytes"] or digest(path) != item["sha256"]:
            raise ValueError("closed workspace copy is incomplete; original Pod retained")
    owned(args.pod)
    subprocess.run(KUBE + NS + ["delete", "pod", args.pod, "--wait=true", "--timeout=60s"], check=True)
    create(SimpleNamespace(pod=args.new_pod, node=old["spec"]["nodeName"], image=old["spec"]["containers"][0]["image"], evidence=args.output / "replacement-context"))
    subprocess.run(KUBE + NS + ["wait", "--for=condition=Ready", "pod/" + args.new_pod, "--timeout=180s"], check=True)
    new = owned(args.new_pod)
    save(args.output / "replacement-pod.json", new)
    if old["metadata"]["uid"] == new["metadata"]["uid"]:
        raise ValueError("replacement is not a distinct Kubernetes Pod")
    subprocess.run(KUBE + NS + ["cp", "--no-preserve", str(donor), args.new_pod + ":" + remote], check=True)
    subprocess.run(KUBE + NS + ["cp", "--no-preserve", str(code), args.new_pod + ":/mnt/fs2-scientific/recovery-qualification"], check=True)
    started = time.monotonic()
    with (args.output / "resume-client.log").open("w") as log:
        subprocess.run(KUBE + NS + ["exec", args.new_pod, "--", "python3", script, "--phase", "resume", "--output", remote, "--job", job], stdout=log, stderr=subprocess.STDOUT, check=True)
    with (args.output / "validation-client.log").open("w") as log:
        validation = subprocess.run(KUBE + NS + ["exec", args.new_pod, "--", "python3", "/mnt/fs2-scientific/recovery-qualification/validate_case.py", remote + "/restored", "--output", remote + "/restored/validation.json"], stdout=log, stderr=subprocess.STDOUT)
    restored = args.output / "after-pod-replacement"
    subprocess.run(KUBE + NS + ["cp", "--retries=3", args.new_pod + ":" + remote, str(restored)], check=True)
    workspace = restored / "restored"
    gpu = subprocess.check_output(KUBE + NS + ["exec", args.new_pod, "--", "nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"], text=True).strip()
    gpu_name, driver = [item.strip() for item in gpu.split(",")]
    science = json.loads((workspace / "validation.json").read_text())
    receipt = {"schema": "fs2-serve.nebius.ai/amber-native-recovery/v1", "recorded_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if validation.returncode == 0 and science["status"] == "passed" else "failed", "model_id": "amber", "runtime_image": new["spec"]["containers"][0]["image"], "image_id": new["status"]["containerStatuses"][0]["imageID"], "case": job, "pool": "l40s-1x" if "L40S" in gpu_name else "h100-ondemand-1x", "gpu_name": gpu_name, "driver": driver, "original_pod_uid": old["metadata"]["uid"], "replacement_pod_uid": new["metadata"]["uid"], "node": new["spec"]["nodeName"], "interrupted_status": json.loads((donor / "interrupted/result.json").read_text())["status"], "closed_generation": manifest["state"]["generation"], "closed_completed_steps": manifest["state"]["completed_steps"], "closed_manifest_sha256": digest(manifest_path), "input_sha256": digest(args.fixture / "input.tar.gz"), "request_sha256": digest(args.fixture / "request.json"), "result_sha256": digest(workspace / "result.json"), "validation_sha256": digest(workspace / "validation.json"), "scientific_validation": science, "resume_and_copy_seconds": time.monotonic() - started, "native_checkpoint_recovery": True, "fresh_pod": True, "customer_transport_tested": False, "customer_ready": False, "gpu_process_snapshot": False, "interruption_kind": "SIGTERM during active PMEMD after an acknowledged closed stage, then original Pod deletion", "raw_evidence": str(args.output)}
    save(args.output / "fresh-pod-recovery.json", receipt)
    print(json.dumps(receipt))
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
