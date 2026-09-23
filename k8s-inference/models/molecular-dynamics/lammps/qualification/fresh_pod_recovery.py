"""Interrupt native dynamics, delete the owned Pod, and restore in a new Pod.

The closed generation is copied via the host. This intentionally does not claim
customer object storage or GPU process snapshot transport.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from cluster import create, owned
from make_fixture import fixture, write_fixture
from mirror_runtime import KUBE
from native_receipt import digest
from validate_case import validate


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--new-pod", required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old = owned(args.pod)
    if args.pod == args.new_pod:
        raise ValueError("use distinct names and Pod UIDs for this qualification")
    # Refuse to destroy an independently running task-owned campaign.
    processes = subprocess.check_output(KUBE + ["exec", args.pod, "--", "ps", "-eo", "args"], text=True)
    if "-m fs2_lammps.worker" in processes or "/bin/lmp -k" in processes:
        raise ValueError("original Pod still has an active native workflow")
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "original-pod.json", old)
    source = args.output / "fixture"
    body, files = fixture("lj", args.assets, 100000, warmup=2000, segment_seconds=5, trajectory_every=5000)
    write_fixture(source, body, files)
    remote = "/mnt/fs2-scientific/native-recovery"
    script = Path(__file__).with_name("interrupt_resume.py")
    subprocess.run(KUBE + ["cp", "--no-preserve", str(source), args.pod + ":/mnt/fs2-scientific/recovery-fixture"], check=True)
    subprocess.run(KUBE + ["cp", "--no-preserve", str(script), args.pod + ":/mnt/fs2-scientific/interrupt_resume.py"], check=True)
    with (args.output / "interrupt-client.log").open("wb") as log:
        subprocess.run(KUBE + ["exec", args.pod, "--", "python3", "/mnt/fs2-scientific/interrupt_resume.py", "--phase", "interrupt", "--input", "/mnt/fs2-scientific/recovery-fixture", "--output", remote, "--job", "lj"], stdout=log, stderr=subprocess.STDOUT, check=True)
    interrupted = args.output / "before-pod-deletion"
    subprocess.run(KUBE + ["cp", args.pod + ":" + remote, str(interrupted)], check=True)
    manifest_path = interrupted / "restored/.fs2/closed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        path = interrupted / "restored/data" / entry["path"]
        if path.stat().st_size != entry["size_bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("host copy of the closed generation is incomplete; original Pod retained")
    # Evidence and all closed files have now been validated outside the Pod.
    owned(args.pod)
    subprocess.run(KUBE + ["delete", "pod", args.pod, "--wait=true", "--timeout=60s"], check=True)
    create(SimpleNamespace(pod=args.new_pod, node=old["spec"]["nodeName"], image=old["spec"]["containers"][0]["image"], max_active_gpu_pods=2))
    subprocess.run(KUBE + ["wait", "--for=condition=Ready", "pod/" + args.new_pod, "--timeout=180s"], check=True)
    new = owned(args.new_pod)
    save(args.output / "replacement-pod.json", new)
    if old["metadata"]["uid"] == new["metadata"]["uid"]:
        raise ValueError("Kubernetes did not create a distinct worker Pod")
    subprocess.run(KUBE + ["cp", "--no-preserve", str(interrupted), args.new_pod + ":" + remote], check=True)
    subprocess.run(KUBE + ["cp", "--no-preserve", str(script), args.new_pod + ":/mnt/fs2-scientific/interrupt_resume.py"], check=True)
    start = time.monotonic()
    with (args.output / "resume-client.log").open("wb") as log:
        subprocess.run(KUBE + ["exec", args.new_pod, "--", "python3", "/mnt/fs2-scientific/interrupt_resume.py", "--phase", "resume", "--output", remote, "--job", "lj"], stdout=log, stderr=subprocess.STDOUT, check=True)
    restored = args.output / "after-pod-replacement"
    subprocess.run(KUBE + ["cp", args.new_pod + ":" + remote, str(restored)], check=True)
    validation = validate(restored / "restored")
    save(args.output / "scientific-validation.json", validation)
    receipt = {"status": "passed" if validation["status"] == "passed" else "failed", "model_id": "lammps", "runtime_image": new["spec"]["containers"][0]["image"], "original_pod_uid": old["metadata"]["uid"], "replacement_pod_uid": new["metadata"]["uid"], "original_node": old["spec"]["nodeName"], "replacement_node": new["spec"]["nodeName"], "interrupted_status": json.loads((interrupted / "interrupted/result.json").read_text())["status"], "closed_generation": manifest["state"]["generation"], "closed_native_step": manifest["state"]["active_step"]["progress"], "closed_manifest_sha256": digest(manifest_path), "resumed_result_sha256": digest(restored / "restored/result.json"), "validation_sha256": digest(args.output / "scientific-validation.json"), "resume_and_copy_seconds": time.monotonic() - start, "fresh_pod": True, "native_checkpoint_recovery": True, "gpu_process_snapshot": False, "customer_transport_tested": False, "customer_ready": False, "interruption_kind": "SIGTERM during active native continuation followed by original Pod deletion", "raw_evidence": str(args.output)}
    save(args.output / "fresh-pod-recovery.json", receipt)
    print(json.dumps(receipt))
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
