"""Bind the new warmed capture to retained isolated evidence; never apply it.

This does not inherit the historical r7 qualification and does not select a
public cache policy. A renderer-generated strict restore and public rollout
remain separate gates from the measurements used to prepare this bundle.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from fractions import Fraction
from pathlib import Path

from fs2_serve.serving_snapshot import ServingSnapshotBundle, configure_serving_snapshot

ROOT = Path(__file__).resolve().parents[2]
IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cosmos3-contract@sha256:"
    "5e2680aa1f8332413638ec1bc962c3796a79a314c1c84f5456d32aa916839e32"
)
BUNDLE_ID = "cosmos3-nano-h100-cuda-criu-warmed-20260918"
STORAGE = "science-cosmos-warmed-20260918-v4-quiet/cosmos-r7"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def validate_inventory(inventory):
    if inventory["status"] != "passed" or not inventory["read_only"] or inventory["storage_path"] != STORAGE:
        raise ValueError("expected successful read-only inventory of the unique new capture")
    manifest = inventory["manifest"]
    if hashlib.sha256(canonical(manifest)).hexdigest() != inventory["manifest_sha256"]:
        raise ValueError("snapshot inventory digest differs")
    entries = manifest["entries"]
    if len(entries) != inventory["entry_count"] or len({row["path"] for row in entries}) != len(entries):
        raise ValueError("snapshot inventory entry set differs")
    if sum(row.get("bytes", 0) for row in entries) != inventory["bytes"]:
        raise ValueError("snapshot inventory byte count differs")
    worker = next(row for row in entries if row["path"] == "worker.log")
    return {key: worker[key] for key in ("bytes", "sha256", "mode", "uid", "gid")}


def restore_record(folder, identity):
    pod = read(folder / "pod-final.json")
    runtime = next(c for c in pod["spec"]["containers"] if c["name"] == "vllm-omni")
    if runtime["image"] != IMAGE or runtime["command"][runtime["command"].index("--fallback") + 1] != "fail":
        raise ValueError("isolated restore must use exact image and forbid normal-load fallback")
    if any(c["restartCount"] for c in pod["status"]["containerStatuses"]):
        raise ValueError("isolated restore restarted")
    log = (folder / "runtime-final.log").read_text()
    restored, _ = json.JSONDecoder().raw_decode(log.lstrip())
    if restored["status"] != "passed" or restored["action"] != "restore":
        raise ValueError("strict runtime restore did not pass")
    for key, value in identity.items():
        if key != "gpu_uuid" and restored["runtime_identity"][key] != value:
            raise ValueError("restored runtime identity differs: " + key)
    response = read(folder / "response-receipt.json")
    if response["status_code"] != 200 or response["body_sha256"] != sha(folder / "output.mp4"):
        raise ValueError("original recorded output failed or changed")
    records = restored["records"]
    if any(record["returncode"] for record in records):
        raise ValueError("a restore step failed")
    return {
        "trial": folder.name,
        "pod_uid": pod["metadata"]["uid"],
        "node": pod["spec"]["nodeName"],
        "gpu_uuid": restored["runtime_identity"]["gpu_uuid"],
        "runtime_receipt_sha256": sha(folder / "runtime-final.log"),
        "pod_receipt_sha256": sha(folder / "pod-final.json"),
        "original_response_sha256": sha(folder / "response-receipt.json"),
        "original_output_sha256": response["body_sha256"],
        "criu_restore_seconds": sum(r["seconds"] for r in records if "/tools/criu" in r["command"]),
        "cuda_restore_seconds": sum(
            r["seconds"] for r in records if r["command"][:3] == ["/tools/cuda-checkpoint", "--action", "restore"]
        ),
        "strict_restore_no_fallback": True,
    }


def verify_output(folder):
    receipt, request, probe = (read(folder / name) for name in ("receipt.json", "request.json", "ffprobe.json"))
    if receipt["status_code"] != 200 or not receipt["valid"] or receipt["sha256"] != sha(folder / "output.mp4"):
        raise ValueError("retained broader video output failed or changed")
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    width, height = map(int, request["size"].split("x"))
    actual = video["width"], video["height"], int(video["nb_read_frames"]), Fraction(video["avg_frame_rate"])
    if actual != (width, height, request["num_frames"], Fraction(request["fps"])):
        raise ValueError("retained broader video shape differs from request")
    return {"receipt_sha256": sha(folder / "receipt.json"), "output_sha256": receipt["sha256"]}


def prepare(evidence, maps, owner, renderer_evidence=None):
    inventory = read(evidence / "snapshot-inventory/receipt.json")
    worker = validate_inventory(inventory)
    capture_path = evidence / "isolated-fresh-warmquiet/capture-r1-receipt.json"
    capture = read(capture_path)
    identity = capture["runtime_identity"]
    if capture["status"] != "passed" or identity["runtime_image"] != IMAGE:
        raise ValueError("new exact-image capture is not complete")
    if inventory["compatibility"]["runtime_identity"] != identity:
        raise ValueError("captured file compatibility differs from the completed capture")
    donor = read(evidence / "isolated-fresh-warmquiet/pod-final.json")
    restores = [
        restore_record(evidence / name, identity)
        for name in ("isolated-restore-warmquiet", "isolated-restore-warmoptions", "isolated-restore-crossnode")
    ]
    if len({r["gpu_uuid"] for r in restores}) < 2 or len({r["original_output_sha256"] for r in restores}) != 1:
        raise ValueError("different-GPU identical-output restore evidence is required")
    parity = read(evidence / "isolated-restore-crossnode/cross-gpu-parity.json")
    if (
        not parity["original_output_equal"]
        or len(parity["cases"]) != 8
        or not all(row["equal_sha256"] and row["candidate_valid"] for row in parity["cases"])
    ):
        raise ValueError("broader cross-GPU parity is incomplete")
    cases = []
    for row in parity["cases"]:
        relative = Path("unseen-controls") / row["case"]
        first = verify_output(evidence / "isolated-restore-warmoptions" / relative)
        second = verify_output(evidence / "isolated-restore-crossnode" / relative)
        if first["output_sha256"] != second["output_sha256"]:
            raise ValueError("cross-GPU output content differs from the parity receipt")
        cases.append({"case": row["case"], "same_gpu": first, "different_gpu": second})
    report = {
        "schema": "fs2-serve.nebius.ai/serving-snapshot-qualification/v1",
        "model_id": "cosmos3-nano",
        "date": "2026-09-18",
        "status": "isolated-exact-runtime-qualified",
        "production_selectable": False,
        "qualification_scope": (
            "Three strict restored Pod lifecycles on two physical H100 GPUs; "
            "renderer-generated and public rollout acceptance remain separate"
        ),
        "mechanism": "cuda-criu",
        "default": "normal-load",
        "compatibility": inventory["compatibility"],
        "bundle": {key: inventory[key] for key in ("manifest_sha256", "bytes", "file_count", "entry_count")},
        "capture_receipt_sha256": sha(capture_path),
        "inventory_receipt_sha256": sha(evidence / "snapshot-inventory/receipt.json"),
        "capture": {
            "cuda_checkpoint_seconds": sum(
                r["seconds"]
                for r in capture["records"]
                if r["command"][:3] == ["/tools/cuda-checkpoint", "--action", "checkpoint"]
            ),
            "criu_dump_seconds": sum(r["seconds"] for r in capture["records"] if "/tools/criu" in r["command"]),
            "flush_seconds": capture["checkpoint_flush_seconds"],
        },
        "restores": restores,
        "different_gpu_cases": cases,
        "cross_gpu_parity_receipt_sha256": sha(evidence / "isolated-restore-crossnode/cross-gpu-parity.json"),
        "limitations": [
            "Same runtime image, precision, checkpoint, H100 class, driver and kernel only; "
            "other GPU families remain unqualified.",
            "Original r7 and failed verbose-capture directories are not selected or overwritten.",
            "Approximately 37-second container-to-ready measurements retain host/image caches; "
            "cross-node image pull cost was separately 164.59 seconds.",
            "First-frame V2V may change physical motion. Transfer is not established as "
            "lighting-only or action-safe training augmentation.",
            "Explicit edge/blur preset adapter qualification is separate from the immutable public V4 default adapter.",
            "No public owner cache policy, replica setting or serving route is modified by preparation.",
        ],
    }
    if renderer_evidence is not None:
        rendered = restore_record(renderer_evidence, identity)
        if rendered["original_output_sha256"] != restores[0]["original_output_sha256"]:
            raise ValueError("production renderer changed the restored original output")
        compared = []
        for name in ("v2v-41-512x288-seed20260930", "t2v-9-256x256-fps24"):
            verified = verify_output(renderer_evidence / "unseen" / name)
            if (
                verified["output_sha256"]
                != verify_output(evidence / "isolated-restore-crossnode/unseen-controls" / name)["output_sha256"]
            ):
                raise ValueError("production renderer changed the restored unseen output")
            compared.append({"case": name, **verified})
        report["renderer_qualification"] = {
            **rendered,
            "unseen_cases": compared,
            "production_template_digest": owner["spec"]["runtime"]["templateRef"]["digest"],
        }
        report["qualification_scope"] = (
            "Three strict isolated restore lifecycles on two physical H100 GPUs, plus a fourth "
            "strict lifecycle from the real production renderer. Public rollout remains separate."
        )
    report_bytes = json.dumps(report, indent=2).encode() + b"\n"
    original = read(ROOT / "acceptance/h100-fleet/snapshots/cosmos3-nano-bundle.json")
    current = owner["spec"]
    if current["runtime"]["image"] != IMAGE or current["artifact"]["revision"] != identity["model_revision"]:
        raise ValueError("current runtime differs from new capture")
    bundle = copy.deepcopy(original)
    bundle.update(
        bundle_id=BUNDLE_ID,
        runtime_image=IMAGE,
        artifact_manifest_digest=current["artifact"]["manifestDigest"],
        manifest_sha256=inventory["manifest_sha256"],
        qualification_receipt_sha256=hashlib.sha256(report_bytes).hexdigest(),
        source_configmap="fs2-cosmos-quiet-capture-diagnostic-20260918",
        source_sha256=identity["snapshot_source_sha256"],
        entrypoint_configmap="fs2-cosmos-log-bridge-diagnostic-20260918",
        entrypoint_sha256=sha(ROOT / "models/scientific-snapshot/serving_entrypoint_logging.py"),
        bundle_path=STORAGE,
        captured_directory="cosmos-r7",
        captured_pod_ip=donor["status"]["podIP"],
        worker_log=worker,
    )
    config = ServingSnapshotBundle.model_validate(bundle)
    templates = next(
        json.loads(cm["data"]["renderer-bundles.json"]) for cm in maps["items"] if "renderer-bundles.json" in cm["data"]
    )
    template = next(t for t in templates if t["templateDigest"] == current["runtime"]["templateRef"]["digest"])
    deployment = next(r for r in template["resources"] if r["kind"] == "Deployment")
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "fs2-cosmos-rendered-snapshot-20260918",
            "namespace": "fs2-models",
            "labels": {"qualification.fs2.nebius/task": "cosmos-recorded-video-20260918"},
        },
        "spec": copy.deepcopy(deployment["spec"]["template"]["spec"]),
    }
    configure_serving_snapshot(pod["spec"], config=config, runtime_container_name="vllm-omni", fallback="fail")
    pod["spec"]["restartPolicy"] = "Never"
    if renderer_evidence is not None:
        submitted = read(renderer_evidence / "renderer-pod.json")
        if submitted != pod:
            raise ValueError("measured production renderer input differs from prepared optional bundle")
    return report_bytes, bundle, pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("evidence", "maps", "owner", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--renderer-evidence", type=Path)
    args = parser.parse_args()
    report, bundle, pod = prepare(args.evidence, read(args.maps), read(args.owner), args.renderer_evidence)
    args.output.mkdir(exist_ok=False)
    (args.output / "qualification.json").write_bytes(report)
    for name, value in (("bundle.json", bundle), ("renderer-pod.json", pod)):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "prepared-not-applied",
                "bundle_id": bundle["bundle_id"],
                "manifest_sha256": bundle["manifest_sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
