"""Emit an apply_patch from retained private NVIDIA reference qualification.

Only the public NVIDIA sample's redacted media identities and response hashes
enter source control. No credential, customer media or route grant is emitted.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CATALOG = ROOT / "catalog/runtime"
SOURCE_COMMIT = "a75a99b42db5d5dab8602bf492a70a395a5059dd"
SOURCE_TREE = "51809d320b9c70b4bc01276cbf6c43cd5ffdc463"
VLLM_DIGEST = "sha256:7a0f0fdd2771464b6976625c2b2d5dd46f566aa00fbc53eceab86ef50883da90"
ADAPTER = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/general-media/paidf-chat-adapter@sha256:b88368b1473fc0960cd45856d3f8dc781066f42a154263a6369d0a85765ee471"
MODELS = {
    "vlm": ("qwen3-6-27b-fp8", "paidf-qwen36-vllm019-fp8-v1", "Qwen3.6 27B FP8", "NVIDIA-H100-SXM5-80GB", 96),
    "llm": ("qwen2-5-14b-instruct", "paidf-qwen25-vllm019-bf16-v1", "Qwen2.5 14B Instruct", "NVIDIA-L40S-48GB", 44),
}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads((args.qualification / "result.json").read_text())
    requests = json.loads((args.qualification / "requests.json").read_text())["calls"]
    if not result["through_adapters"] or not result["source_unchanged"] or result["requests"] != 4:
        raise ValueError("Incomplete exact-source reference qualification")
    outputs = {}
    evidence_path = HERE / "reference-chat-qualification-20260920.json"
    evidence = {"result": result, "calls": requests,
                "limitations": ["Private GPU/native adapter qualification, not public App or workbench E2E.",
                                "The retained caption contained repetitive text; no text was cut or rewritten before the original LLM prompt stage.",
                                "The clear-weather source correctly failed the requested cloudy attribute check."]}
    outputs[evidence_path] = json.dumps(evidence, indent=2) + "\n"
    semantic_path = "k8s-inference/models/general-media/paidf-chat/qualify_reference.py"
    fixture_path = "k8s-inference/models/general-media/paidf-chat/reference-chat-qualification-20260920.json"
    outputs[CATALOG / "packaged-repository" / semantic_path] = (HERE / "qualify_reference.py").read_text()
    outputs[CATALOG / "packaged-repository" / fixture_path] = outputs[evidence_path]
    for role, (app_id, variant, display, gpu, ram_gib) in MODELS.items():
        weights = json.loads((HERE / (role + "-loaded-files-20260920.json")).read_text())
        model, revision = weights["model"], weights["revision"]
        if model != result["models"][role] or revision != result["model_revisions"][role]:
            raise ValueError("Weights differ from the exercised reference model")
        calls = [call for call in requests if call["body"]["model"] == model]
        if len(calls) != 2 or any(call["response"]["status"] != 200 for call in calls):
            raise ValueError("Two successful distinct reference requests are required")
        if len({call["raw_request_sha256"] for call in calls}) != 2 or len({call["response"]["body_sha256"] for call in calls}) != 2:
            raise ValueError("Semantic request/response identities must differ")
        artifact = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1", "model_id": app_id, "kind": "weights",
            "source": {"uri": "hf://" + model, "revision": revision},
            "content": {"digest": digest(weights["files"]), "expanded_bytes": weights["expanded_bytes"], "files": weights["files"]},
            "license": {"id": "Apache-2.0", "state": "verified"}, "entitlement_state": "not-required",
            "owner": "platform-pvc", "retention": "retained-platform",
        }
        manifest_digest = digest(artifact)
        artifact_name = f"{app_id}-{manifest_digest}.json"
        outputs[CATALOG / "deployment-runtimes/artifacts" / artifact_name] = json.dumps(artifact, indent=2) + "\n"
        argv = ["vllm", "serve", model, "--revision", revision, "--host", "127.0.0.1", "--port", "8001", "--max-num-seqs", "1", "--gpu-memory-utilization", "0.85"]
        if role == "vlm":
            argv.extend(["--reasoning-parser", "qwen3"])
        record = {
            "schema": "fs2-serve.nebius.ai/model/v1",
            "model": {"id": app_id, "display_name": display, "family": "llm", "tested_lane": True,
                      "source": {"kind": "huggingface", "repository": model, "revision": revision,
                                 "license": {"id": "Apache-2.0", "state": "verified", "notes": "The exact Qwen model card declares Apache-2.0; source weights and tokenizer files are pinned and hash-verified."},
                                 "entitlement": {"required": False, "state": "not-required", "credential_contract": None, "notes": "Public ungated checkpoint; no Hugging Face credential is required."}}},
            "runtime": {"kind": "custom", "version": "vllm-0.19.0-paidf-native-v1", "image": {"reference": "docker.io/vllm/vllm-openai@" + VLLM_DIGEST, "digest": VLLM_DIGEST, "state": "resolved"}, "command": argv},
            "resources": {"cpu_millis": 8500, "memory_bytes": ram_gib * 1024**3 + 512 * 1024**2, "host_ram_min_bytes": None,
                          "scaler_owner": "nebius-managed-node-group-autoscaler", "gpu": {"class": gpu, "count": 1, "topology": "single-gpu", "placement": None, "b300_state": "unverified", "alternatives": []}},
            "interface": {"execution_mode": "http", "protocols": ["native"], "endpoints": {"native": "/v1/reference-chat"},
                          "readiness": {"method": "GET", "path": "/v1/health/ready", "expected_status": 200, "timeout_seconds": 2700}, "warmup": None,
                          "policy": {"operations": ["reference-chat"], "license_enforced": True, "non_clinical": True, "commercial_use": "allowed"},
                          "mcp": {"discoverable": True, "invocable": False}},
            "startup": {"default": "conventional", "fallback": "conventional", "enabled_mechanisms": ["conventional"], "experiments": [], "multi_gpu_criu": "unproven-disabled"},
            "cache": {"owner": "platform-pvc", "shared_path": "/mnt/fs2-serve-cache/models/" + app_id, "local_path": "/var/lib/fs2-serve/cache/models/" + app_id, "pre_pull_image": True,
                      "artifact": {"state": "platform-verified", "kind": "weights", "manifest_digest": manifest_digest, "expanded_bytes": weights["expanded_bytes"], "minimum_bytes": weights["expanded_bytes"], "capacity_bound_bytes": 80 * 1024**3, "staged": False}},
            "semantic_validator": {"kind": "repository-command", "contract": "nvidia-paidf-exact-reference-chat/v1", "source_path": semantic_path, "source_sha256": hashlib.sha256(outputs[CATALOG / "packaged-repository" / semantic_path].encode()).hexdigest(), "fixture_path": fixture_path, "fixture_sha256": hashlib.sha256(outputs[evidence_path].encode()).hexdigest(), "request_count": 2, "distinct_requests": True, "distinct_responses": True},
            "support": {"state": "qualified", "route_exposed": False, "non_clinical": True,
                        "limitations": ["Direct private runtime qualification only; managed App, public HTTP/MCP and workbench E2E are separate gates.",
                                        "Exact source revision and native precision only; no model/provider fallback, quantization conversion, snapshots or scale-to-zero qualification.",
                                        "The adapter preserves the original OpenAI JSON/SSE bytes over durable native operations: " + ADAPTER,
                                        "One in-flight request; explicit transport bounds: 128 MiB video, 16 MiB image, 192 MiB JSON request, 16 MiB response. No silent media sampling or resizing.",
                                        "NVIDIA reference caption output can be repetitive; the measured caption is retained unchanged. Model output is not ground truth."]},
            "evidence": [{"classification": "measured-platform", "hardware": weights["hardware"], "outcome": "live-qualified", "source_commit": SOURCE_COMMIT,
                          "summary": f"Two distinct original NVIDIA requests completed through the immutable native adapter on the private {gpu} probe. Full-source SHA256 {result['source_sha256']}; 153 frames at 1920x1080, unchanged. Original caption/prompt/attribute implementation pinned at {result['nvidia_revision']}. Retained evidence SHA256 {digest(evidence)}. No public-route, managed cold-start or complete workbench qualification is claimed."}],
            "provenance": [{"commit": SOURCE_COMMIT, "tree": SOURCE_TREE, "path": semantic_path, "classification": "measured-handoff"},
                           {"url": "https://huggingface.co/" + model + "/tree/" + revision, "revision": revision, "classification": "reviewed-input"}],
        }
        semantic = {"state": "qualified", "blocker": None, "serialization": "sha256-json-compact-no-newline/v1",
                    "invocation": {"operation": "reference-chat", "protocol": "native", "method": "POST", "endpoint": "/v1/reference-chat"},
                    "requests": [{"id": f"{app_id}-reference-{index}", "payload_sha256": call["raw_request_sha256"]} for index, call in enumerate(calls)], "assets": []}
        native = {"schema": "fs2-serve.nebius.ai/native-catalog-model/v1", "record": record, "variant_id": variant, "runtime_architecture": "cuda", "semantic_requests": semantic,
                  "artifact_manifest": {"path": "../deployment-runtimes/artifacts/" + artifact_name, "sha256": manifest_digest}}
        outputs[CATALOG / "native" / (app_id + ".json")] = json.dumps(native, indent=2) + "\n"
        qualification = {"model_id": app_id, "variant_id": variant,
                         "active_runtime": {"model_revision": revision, "runtime_image_digest": VLLM_DIGEST, "service": {"name": app_id, "namespace": "fs2-models", "port": 8000}},
                         "runtime_origin": {"kind": "independent-runtime", "variant_id": variant, "source_kind": "huggingface", "repository": model, "relationship": "exact-model", "nim_artifact_parity": "not-applicable"},
                         "states": {"registered": True, "route_active": False, "runtime_ready": True, "semantic_qualified": True, "http_mcp_qualified": False, "cold_start_qualified": False, "elasticity_qualified": False},
                         "policy": {"license_id": "Apache-2.0", "non_clinical": True, "commercial_use": "allowed"},
                         "evidence": {"audited_catalog_sha256": digest(record), "retained_deployments_sha256": digest(evidence), "audited_live_routes_sha256": None, "model_discovery_sha256": None, "mcp_discovery_sha256": None, "http_mcp_acceptance_sha256": None, "cold_start_acceptance_sha256": None, "elasticity_acceptance_sha256": None}}
        entry = {"schema": "fs2-serve.nebius.ai/deployment-runtime/v1", "model_id": app_id, "variant_id": variant, "record": copy.deepcopy(record), "qualification": qualification}
        outputs[CATALOG / "deployment-runtimes" / (app_id + ".json")] = json.dumps(entry, indent=2) + "\n"
    print("*** Begin Patch")
    for path, value in outputs.items():
        if path.exists():
            raise ValueError("Refusing to replace an existing declaration: " + str(path))
        print("*** Add File: " + str(path))
        print("\n".join("+" + line for line in value.rstrip("\n").split("\n")))
    print("*** End Patch")


if __name__ == "__main__":
    main()
