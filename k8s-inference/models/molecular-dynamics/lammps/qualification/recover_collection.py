"""Record a manually recovered frozen workspace without rerunning native science."""

import argparse
import json
from pathlib import Path

from cluster import owned
from native_receipt import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pod = owned(args.pod)
    result_path = args.output / "workspace/result.json"
    result = json.loads(result_path.read_text())
    execution = json.loads((args.output / "workspace/execution.json").read_text())
    for entry in result["files"]:
        path = args.output / "workspace/data" / entry["path"]
        if path.stat().st_size != entry["size_bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("recovered files disagree with the native worker's result inventory")
    receipt = {"pod": args.pod, "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "image": pod["spec"]["containers"][0]["image"], "image_id": pod["status"]["containerStatuses"][0]["imageID"], "created_at": pod["metadata"]["creationTimestamp"], "worker_exit": execution["exit_code"], "job": result["job_id"], "input_copy_seconds": None, "runtime_wall_seconds": execution["elapsed_seconds"], "output_copy_seconds": None, "customer_path_tested": False, "checkpoint_transport": "local-only", "cpu_limit": 8, "gpu_count": 1, "gpu_process_snapshot": "not-tested", "collection_recovery": {"failure": "kubectl cp unexpected EOF after native completion", "original_partial_copy": str(args.output / "workspace.partial-eof"), "native_science_rerun": False, "files_verified_against_result_inventory": True, "unavailable_timing": "original host-boundary timings were not committed before copy error; null is intentional"}}
    destination = args.output / "qualification.json"
    if destination.exists():
        raise ValueError("refusing to overwrite an existing qualification receipt")
    destination.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "collection-recovered", "worker_status": result["status"], "result_sha256": digest(result_path)}))


if __name__ == "__main__":
    main()
