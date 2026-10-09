"""Derive H100 placement evidence from retained public calls; no network or writes to source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLASS = "nvidia-h100-sxm5-80gb"
COHORTS = {"sdxl": ("general-r1", 36), "nv-reason-cxr-3b": ("cxr-schema-attribution-repaired-r1", 20)}
GPU_IDS = "telemetry.fs2.nebius.ai/gpu-uuids"


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def witness(pod, node, operation, record, content_digest):
    runtime = operation["runtime"]
    uid = runtime["pod_uid"]
    annotations, labels = pod["metadata"].get("annotations", {}), node["metadata"]["labels"]
    gpu = [
        c
        for c in pod["spec"]["containers"]
        if c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu") in (1, "1")
    ]
    if len(gpu) != 1:
        raise ValueError("exactly one one-GPU model container required")
    container = gpu[0]
    status = next(s for s in pod["status"]["containerStatuses"] if s["name"] == container["name"])
    digest = record["runtime"]["image"]["digest"]
    if (
        pod["metadata"]["uid"] != uid
        or pod["spec"]["nodeName"] != node["metadata"]["name"]
        or runtime["node_uid"] != node["metadata"]["uid"]
        or runtime["gpu_count"] != 1
        or len(runtime["gpu_uuids"]) != 1
        or json.loads(annotations.get(GPU_IDS, "[]")) != runtime["gpu_uuids"]
        or labels.get("accelerator.fs2.nebius/class") != CLASS
        or labels.get("node.kubernetes.io/instance-type") != "gpu-h100-sxm"
        or labels.get("nebius.com/nvidia_driver_version") != "580.159.04-1ubuntu1"
        or container["image"].split("@")[-1] != digest
        or status["imageID"].split("@")[-1] != digest
        or annotations.get("fs2.nebius/runtime-image-digest") != digest
        or annotations.get("fs2.nebius/model-revision") != record["model"]["source"]["revision"]
        or annotations.get("fs2.nebius/model-content-digest") != "sha256:" + content_digest
        or not status["state"].get("running")
        or timestamp(status["state"]["running"]["startedAt"]) > timestamp(operation["started_at"])
    ):
        raise ValueError("exact image/revision/content/Pod/node/GPU SKU witness mismatch")
    return {
        "pod_uid": uid,
        "node_uid": node["metadata"]["uid"],
        "node_name": node["metadata"]["name"],
        "gpu_uuids": runtime["gpu_uuids"],
        "image": container["image"],
        "actual_image_id": status["imageID"],
        "container_id": status["containerID"],
        "restart_count": status["restartCount"],
        "started_at": status["state"]["running"]["startedAt"],
        "gpu_class": CLASS,
        "provider_instance_type": labels["node.kubernetes.io/instance-type"],
        "driver_version": labels["nebius.com/nvidia_driver_version"],
        "pool": labels["accelerator.fs2.nebius/pool-id"],
    }


def observations(root, start, end):
    result = []
    for folder in sorted((root / "observations").iterdir()):
        try:
            before = datetime.strptime(folder.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if not start - timedelta(minutes=2) <= before <= end + timedelta(minutes=2):
            continue
        pp, np = folder / "pods.json", folder / "nodes.json"
        if not pp.is_file() or not np.is_file():
            continue
        result.append(
            {
                "observed_after": before,
                "observed_before": datetime.fromtimestamp(max(pp.stat().st_mtime, np.stat().st_mtime), UTC),
                "pods": {p["metadata"]["uid"]: p for p in read(pp)["items"]},
                "nodes": {n["metadata"]["uid"]: n for n in read(np)["items"]},
                "sources": {str(p.relative_to(root)): sha(p) for p in (pp, np)},
            }
        )
    return result


def prepare(root, baseline):
    owners = read(baseline / "modeldeployments.json")["items"]
    records, details = [], {}
    for model, (cohort, expected_count) in COHORTS.items():
        catalog_path = ROOT / "catalog/runtime/models" / (model + ".json")
        record = read(catalog_path)
        owner = next(o for o in owners if o["spec"]["modelRef"] == model)
        source_path = ROOT / "models/general-media/evidence" / (model + "-artifact-manifest.json")
        artifact = read(source_path)
        if (
            owner["spec"]["runtime"]["image"].split("@")[-1] != record["runtime"]["image"]["digest"]
            or owner["spec"]["artifact"]["revision"] != record["model"]["source"]["revision"]
            or owner["spec"]["artifact"]["manifestDigest"] != "sha256:" + record["cache"]["artifact"]["manifest_digest"]
            or artifact["source"]["revision"] != record["model"]["source"]["revision"]
            or record["resources"]["gpu"]["count"] != 1
            or record["resources"]["gpu"]["topology"] != "single-gpu"
        ):
            raise ValueError("current owner/catalog/artifact revision mismatch")
        paths = [p for p in (root / "cohorts" / cohort).glob("*/*/operation.json") if read(p).get("model_id") == model]
        calls = [(p, read(p)) for p in paths]
        if len(calls) != expected_count or len({o["id"] for _, o in calls}) != expected_count:
            raise ValueError("incomplete or duplicate public cohort")
        observed = observations(
            root, min(timestamp(o["started_at"]) for _, o in calls), max(timestamp(o["completed_at"]) for _, o in calls)
        )
        rows, unmatched = [], []
        for path, operation in calls:
            evaluation = read(path.with_name("evaluation.json"))
            receipt = read(path.with_name("receipt.json"))
            if (
                operation["status"] != "succeeded"
                or receipt["state"] != "verified"
                or evaluation["service_semantic_pass"] is not True
                or receipt["operation_id"] != operation["id"]
            ):
                raise ValueError("public completion/semantic check failed")
            result_envelope = read(path.with_name("result-envelope.json"))["result"]
            result_ref = result_envelope.get("artifact")
            if result_ref:
                artifact_path = path.with_name(result_ref["artifact_id"] + ".artifact")
                raw = artifact_path.read_bytes()
                if (
                    len(raw) != result_ref["size_bytes"]
                    or hashlib.sha256(raw).hexdigest() != result_ref["sha256"]
                    or json.loads(raw) != read(path.with_name("result.json"))
                ):
                    raise ValueError("downloaded result artifact differs from public content-addressed reference")
            elif result_envelope != read(path.with_name("result.json")):
                raise ValueError("inline result differs from retained public response")
            started, completed = map(timestamp, (operation["started_at"], operation["completed_at"]))
            uid, node_uid = operation["runtime"]["pod_uid"], operation["runtime"]["node_uid"]
            eligible = [x for x in observed if uid in x["pods"] and node_uid in x["nodes"]]
            before = [x for x in eligible if x["observed_before"] <= started]
            after = [x for x in eligible if x["observed_after"] >= completed]
            if not before or not after:
                unmatched.append({"operation_id": operation["id"], "reason": "no_conservative_timestamp_bracket"})
                continue
            ends = [max(before, key=lambda x: x["observed_before"]), min(after, key=lambda x: x["observed_after"])]
            try:
                a, b = [
                    witness(x["pods"][uid], x["nodes"][node_uid], operation, record, artifact["content"]["digest"])
                    for x in ends
                ]
            except ValueError as error:
                unmatched.append({"operation_id": operation["id"], "reason": str(error)})
                continue
            if a != b:
                raise ValueError("runtime identity changed across inference")
            rows.append(
                {
                    "case_id": path.parent.name,
                    "operation_id": operation["id"],
                    "started_at": operation["started_at"],
                    "completed_at": operation["completed_at"],
                    "runtime": a,
                    "result_artifact_sha256": result_ref["sha256"] if result_ref else None,
                    "source_sha256": {
                        **{
                            str(path.with_name(name).relative_to(root)): sha(path.with_name(name))
                            for name in (
                                "operation.json",
                                "receipt.json",
                                "evaluation.json",
                                "result.json",
                                "request.json",
                            )
                        },
                        **ends[0]["sources"],
                        **ends[1]["sources"],
                    },
                }
            )
        if len(rows) < 2:
            raise ValueError("fewer than two independently attributed public inputs")
        details[model] = {
            "public_completed_requests": expected_count,
            "timestamp_bracketed_requests": len(rows),
            "unmatched": unmatched,
            "rows": sorted(rows, key=lambda r: r["case_id"]),
            "catalog_record_sha256": sha(catalog_path),
            "source_artifact_manifest_file_sha256": sha(source_path),
            "content_digest": artifact["content"]["digest"],
            "owner_template": owner["spec"]["runtime"]["templateRef"],
        }
        records.append(
            {
                "model_id": model,
                "revision": record["model"]["source"]["revision"],
                "artifact_manifest_digest": owner["spec"]["artifact"]["manifestDigest"],
                "runtime_image_digest": record["runtime"]["image"]["digest"],
                "runtime_ready": True,
                "semantic_qualified": True,
                "http_mcp_qualified": True,
                "qualified_requests": len(rows),
                "gpu_count": 1,
                "topology": "single-gpu",
            }
        )
    proof = {
        "schema": "fs2.h100-retained-public-placement-evidence/v1",
        "models": details,
        "baseline_owner_list_sha256": sha(baseline / "modeldeployments.json"),
        "scope": (
            "Exact-image one-H100 runtime and public semantic compatibility only; "
            "not clinical efficacy, image prompt fidelity, cold latency, scale-out or snapshots."
        ),
    }
    proof_raw = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    receipt = {
        "schema": "fs2-serve.nebius.ai/h100-runtime-qualification-receipt/v1",
        "authority": "reviewed-live-runtime-qualification",
        "observed_at": max(r["completed_at"] for d in details.values() for r in d["rows"]),
        "accelerator": {
            "class": CLASS,
            "provider_instance_type": "gpu-h100-sxm",
            "driver_version": "580.159.04-1ubuntu1",
        },
        "source_evidence": {"retained_public_placement_sha256": hashlib.sha256(proof_raw).hexdigest()},
        "models": records,
        "scope": proof["scope"],
    }
    return proof, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "baseline", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("new immutable output directory required")
    proof, receipt = prepare(args.root, args.baseline)
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    for name, value in (("proof.json", proof), ("receipt.json", receipt)):
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "models": [
                    {
                        "model": m,
                        "completed": d["public_completed_requests"],
                        "bracketed": d["timestamp_bracketed_requests"],
                        "unmatched": len(d["unmatched"]),
                    }
                    for m, d in proof["models"].items()
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
