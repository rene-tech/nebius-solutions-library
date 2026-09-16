"""Compare complete restored recordings and report phase boundaries honestly."""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent


def elapsed(start, end):
    return (datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def analyze(kind, pods):
    baseline = json.loads((ROOT / (kind + "-restored-http-live-r2.json")).read_text())
    current = json.loads((ROOT / (kind + "-cross-restored-http-live-r3.json")).read_text())
    if current["status"] != "passed" or len(current["measurements"]) != 4:
        raise ValueError("both complete recordings and both transports must finish")
    indexed = {(row["case"], row["mode"]): row for row in baseline["measurements"]}
    comparisons = []
    for row in current["measurements"]:
        previous = indexed[(row["case"], row["mode"])]
        match = (row["input_sha256"] == previous["input_sha256"]
                 and row["result"]["text"] == previous["result"]["text"]
                 and row["result"]["audio_seconds"] == previous["result"]["audio_seconds"])
        comparisons.append({"case": row["case"], "mode": row["mode"], "exact_match": match,
                            "audio_seconds": row["result"]["audio_seconds"], "wall_seconds": row["wall_seconds"]})
    if not all(row["exact_match"] for row in comparisons):
        raise ValueError("cross-node transcription differs; retain and investigate")
    lines = (ROOT / (kind + "-cross-restore-r3.log")).read_text().splitlines()
    lifecycle, _ = json.JSONDecoder().raw_decode("\n".join(line.split(" ", 1)[1] for line in lines))
    marker = next(line for line in lines if '"mechanism": "cuda-criu-restored"' in line).split(" ", 1)[0]
    if lifecycle["status"] != "passed" or any(row["returncode"] for row in lifecycle["records"]):
        raise ValueError("restore did not succeed without normal-load fallback")
    remap = next(row["command"][row["command"].index("--device-map") + 1]
                 for row in lifecycle["records"] if "--device-map" in row["command"])
    source_gpu, target_gpu = remap.split("=")
    if source_gpu == target_gpu:
        raise ValueError("not a different-GPU restore")
    pod = next(p for p in pods if p["metadata"]["name"] == current["pod"])
    started = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "speech")["state"]["running"]["startedAt"]
    manifest = json.loads((ROOT / (kind + "-snapshot-r2-manifest.json")).read_text())
    return {"model": current["model"], "status": "cross-node-complete-recordings-passed",
            "sample_count": 1, "pod": current["pod"], "pod_uid": pod["metadata"]["uid"],
            "node": pod["spec"]["nodeName"], "source_gpu": source_gpu, "target_gpu": target_gpu,
            "restore_call_seconds": [r["seconds"] for r in lifecycle["records"]],
            "restore_call_sum_seconds": sum(r["seconds"] for r in lifecycle["records"]),
            "pod_created_to_restore_marker_seconds": elapsed(pod["metadata"]["creationTimestamp"], marker),
            "container_started_to_restore_marker_seconds": elapsed(started, marker),
            "clock_note": "Restore marker excludes external readiness propagation and inference; cached images/shared storage, no node provisioning or p95 claim",
            "snapshot_bytes": manifest["bytes"], "snapshot_files": manifest["file_count"],
            "snapshot_manifest_sha256": manifest["bundle_sha256"],
            "comparisons": comparisons, "production_snapshots_enabled": False}


if __name__ == "__main__":
    pods = json.loads((ROOT / "cross-restored-pods-before-cleanup-r3.json").read_text())["items"]
    print(json.dumps({"models": [analyze(kind, pods) for kind in ("en", "multi")]}, indent=2))
