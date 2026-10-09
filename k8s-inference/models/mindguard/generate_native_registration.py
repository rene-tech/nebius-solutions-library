"""Print deterministic catalog files derived from the retained MindGuard evidence.

This does not deploy or grant access. Run against the reviewed source tree and
apply its JSON path/content mapping as an ordinary reviewed source patch.
"""

import hashlib
import json
from pathlib import Path

from lifecycle_fixtures import requests

ROOT = Path(__file__).resolve().parent
SOLUTION = ROOT.parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def generate():
    lock = json.loads((ROOT / "public-models.lock.json").read_text())
    template = json.loads((SOLUTION / "catalog/runtime/native/scvi-scanvi.json").read_text())
    fixture = "k8s-inference/models/mindguard/lifecycle_fixtures.py"
    fixture_content = (ROOT / "lifecycle_fixtures.py").read_text()
    fixture_hash = hashlib.sha256(fixture_content.encode()).hexdigest()
    output = {"k8s-inference/catalog/runtime/packaged-repository/" + fixture: fixture_content}
    for model_id, source in lock["models"].items():
        inventory = json.loads((ROOT / "artifacts" / (model_id + ".json")).read_text())
        assert inventory["revision"] == source["revision"] and inventory["repo_id"] == source["repo_id"]
        files = sorted([{"path": row["path"], "bytes": row["size_bytes"], "sha256": row["sha256"]}
                        for row in inventory["files"]], key=lambda row: row["path"])
        total = sum(row["bytes"] for row in files)
        artifact = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1", "model_id": model_id, "kind": "weights",
            "source": {"uri": "https://huggingface.co/" + source["repo_id"] + "/tree/" + source["revision"],
                       "revision": source["revision"]},
            "content": {"digest": digest(files), "expanded_bytes": total, "files": files},
            "license": {"id": "CC-BY-NC-SA-4.0", "state": "verified"},
            "entitlement_state": "verified", "owner": "platform-pvc", "retention": "retained-platform",
        }
        artifact_digest = digest(artifact)
        artifact_name = model_id + "-" + artifact_digest + ".json"
        output["k8s-inference/catalog/runtime/native/artifacts/" + artifact_name] = json.dumps(artifact, indent=2) + "\n"
        native = json.loads(json.dumps(template))
        record = native["record"]
        record["model"] = {
            "id": model_id, "display_name": "MindGuard " + model_id.rsplit("-", 1)[1].upper(),
            "family": "llm", "tested_lane": True,
            "source": {"kind": "huggingface", "repository": source["repo_id"], "revision": source["revision"],
                       "license": {"id": "CC-BY-NC-SA-4.0", "state": "verified",
                                   "notes": "Pinned Sword model card declares non-commercial share-alike terms. Existing per-model grants apply."},
                       "entitlement": {"required": True, "state": "verified",
                                       "credential_contract": "huggingface-gated-access/v1",
                                       "notes": "Existing authorized gated checkpoint is retained in the platform PVC; credentials are not catalog data."}},
        }
        record["runtime"] = {
            "kind": "custom", "version": "vllm-0.28.0-cu130-bf16-32k-observational",
            "image": {"reference": lock["runtime_image"],
                      "digest": "sha256:" + lock["runtime_image"].split("sha256:")[1], "state": "resolved"},
            "command": ["vllm", "serve", "/models/" + model_id + "/" + source["revision"]],
        }
        record["resources"].update(cpu_millis=4000, memory_bytes=32 * 1024 ** 3)
        record["resources"]["gpu"].update({"class": "NVIDIA-L40S-48GB", "count": 1})
        record["interface"].update(endpoints={"native": "/v1/chat/completions"},
                                   readiness={"method": "GET", "path": "/health", "expected_status": 200,
                                              "timeout_seconds": 1200})
        record["interface"]["policy"].update(operations=["assess-transcript"], commercial_use="prohibited")
        record["startup"]["experiments"] = []
        record["cache"] = {
            "owner": "platform-pvc", "shared_path": "/mnt/fs2-serve-cache/models/" + model_id,
            "local_path": "/var/lib/fs2-serve/cache/models/" + model_id, "pre_pull_image": True,
            "artifact": {"state": "platform-verified", "kind": "weights", "manifest_digest": artifact_digest,
                         "expanded_bytes": total, "minimum_bytes": total, "capacity_bound_bytes": total, "staged": False},
        }
        record["semantic_validator"] = {
            "kind": "repository-command", "contract": "mindguard-transcript-prefix-coverage/v1",
            "source_path": fixture, "source_sha256": fixture_hash,
            "fixture_path": fixture, "fixture_sha256": fixture_hash,
            "request_count": 2, "distinct_requests": True, "distinct_responses": True,
        }
        record["support"] = {
            "state": "qualified", "route_exposed": False, "non_clinical": True,
            "limitations": [
                "Existing exact L40S BF16 runtime and 32k checkpoint behavior are retained; durable cold lifecycle is qualified separately.",
                "English observational safety labels only, one prefix per user turn; no diagnosis, clinician grading, blocking or conversation rewriting.",
                "GPU snapshots are not qualified for this tuple. Non-commercial checkpoint terms and existing per-model grants apply.",
            ],
        }
        evidence_file = ROOT / "evidence" / (model_id.rsplit("-", 1)[1] + "-l40s-32k-representative-c1.json")
        evidence = json.loads(evidence_file.read_text())
        assert evidence["runtime_image"] == lock["runtime_image"] and evidence["model_revision"] == source["revision"]
        record["evidence"] = [{
            "classification": "measured-platform", "hardware": "NVIDIA L40S 48GB, BF16, vLLM 0.28.0, CUDA13.0",
            "outcome": "live-qualified", "source_commit": "d238f9427379c0e96583ba7595a804c391ef0959",
            "summary": "Retained exact-runtime representative prefix evidence: " + evidence_file.name +
                       " SHA256 " + hashlib.sha256(evidence_file.read_bytes()).hexdigest() +
                       ". This is functional classifier evidence, not new cold-start or clinical accuracy qualification.",
        }]
        record["provenance"] = [{"classification": "reviewed-input", "revision": source["revision"],
                                 "url": artifact["source"]["uri"]}]
        native["variant_id"] = model_id + "-vllm-bf16-l40s-v1"
        native["runtime_architecture"] = "cuda"
        native["semantic_requests"] = {
            "state": "qualified", "blocker": None, "serialization": "sha256-json-compact-no-newline/v1",
            "invocation": {"operation": "assess-transcript", "protocol": "native", "method": "POST",
                           "endpoint": "/v1/chat/completions"},
            "requests": [{"id": model_id + "-transcript-" + str(i),
                          "payload_sha256": hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()}
                         for i, body in enumerate(requests(model_id))], "assets": [],
        }
        native["artifact_manifest"] = {"path": "artifacts/" + artifact_name, "sha256": artifact_digest}
        output["k8s-inference/catalog/runtime/native/" + model_id + ".json"] = json.dumps(native, indent=2) + "\n"
    return output


if __name__ == "__main__":
    print(json.dumps(generate()))
