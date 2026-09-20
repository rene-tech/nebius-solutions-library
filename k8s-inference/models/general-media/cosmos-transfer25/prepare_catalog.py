"""Render the native Transfer App from two structurally and semantically valid results.

Prints a filename-to-JSON mapping; does not edit or publish the catalog. Native
runtime qualification is not PAIDF acceptance, customer, HTTP/MCP, workbench or
batch acceptance. Correct weather and frame geometry qualify the native mode;
the unchanged PAIDF motion threshold can still reject that clip for a batch.
"""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = "cosmos-transfer2-5-2b"
VARIANT = "cosmos-transfer25-nim-v1"
NIM_DIGEST = "sha256:1891a2421b57cd5f2249f0b44a2720bbca24804e8e579af297a90c876d62659f"
IMAGE = "nvcr.io/nim/nvidia/cosmos-transfer2.5-2b@" + NIM_DIGEST
ADAPTER = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/general-media/"
    "cosmos-transfer25-adapter@sha256:1a14cccb640d08ae5775f6ebcb9e2fe766f08b717e43526e30057a8c68edfadd"
)
SOURCE_COMMIT = "5dcf2568134517d9be56c7368a8450e91a5e50a6"
SOURCE_TREE = "175d90814081bff938588801fb7f2a3441249282"
PROFILE = "e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672"
LICENSE = "NVIDIA-Open-Model-License AND NVIDIA-Software-License-Agreement"


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prepare(results):
    if len(results) != 2:
        raise ValueError("exactly two distinct retained adapter results are required")
    evidence = []
    for index, result in enumerate(results):
        generation_raw = (result / "receipt.json").read_bytes()
        generation = json.loads(generation_raw)
        quality = json.loads((result / "quality.json").read_text())
        identity = json.loads((result / "runtime-identity.json").read_text())
        if (
            identity.get("image") != ADAPTER
            or identity.get("image_config_digest")
            != "sha256:d13b18d430788078702fb6e0f5cfd19fd0ccf0d4487224dbc135d7cb8291e1b5"
            or identity.get("source_commit") != SOURCE_COMMIT
            or identity.get("container_id") != "58f4cf6aead96b34c16491c0764ea9668ca9a60c324bbb87a266bf385a7d1f7a"
            or identity.get("generation_receipt_sha256") != hashlib.sha256(generation_raw).hexdigest()
            or not datetime.fromisoformat(identity["started_at"])
            <= datetime.fromisoformat(generation["started_at"])
            <= datetime.fromisoformat(identity["observed_at"])
        ):
            raise ValueError("retained running-container observation does not bind this exact generation")
        if (
            generation.get("schema") != "fs2-serve.nebius.ai/cosmos-transfer25-adapter-probe/v1"
            or generation.get("adapter_image") != ADAPTER
            or generation.get("alignment_passed") is not True
            or generation.get("http_status") != 200
            or generation.get("profile_id") != PROFILE
            or quality.get("alignment_passed") is not True
            or quality.get("weather", {}).get("passed") is not True
            or quality.get("generation_receipt_sha256") != hashlib.sha256(generation_raw).hexdigest()
            or quality.get("output_sha256") != generation["output"]["sha256"]
            or quality.get("source_sha256") != generation["source"]["sha256"]
            or quality.get("motion", {}).get("threshold") != 0.682
        ):
            raise ValueError("a result lacks exact adapter, structural, weather and unchanged motion-check evidence")
        if hashlib.sha256((result / "output.mp4").read_bytes()).hexdigest() != quality["output_sha256"]:
            raise ValueError("retained output does not match its evidence")
        evidence.append(
            {
                "id": f"transfer-weather-{index}",
                "generation": generation,
                "runtime_identity": identity,
                "quality": quality,
            }
        )
    if len({row["generation"]["request_sha256"] for row in evidence}) != 2:
        raise ValueError("duplicate semantic requests")
    if len({row["generation"]["output"]["sha256"] for row in evidence}) != 2:
        raise ValueError("duplicate semantic outputs")
    inventory = json.loads((HERE / "workspace-inventory-20260920.json").read_text())
    if inventory["profile_id"] != PROFILE or len(inventory["files"]) != 194:
        raise ValueError("workspace inventory does not match the pinned profile")
    revision = "sha256:" + inventory["manifest_sha256"]
    manifest = {
        "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
        "model_id": MODEL,
        "kind": "nim-cache",
        "source": {"uri": "oci://" + IMAGE, "revision": revision},
        "content": {
            "digest": digest(inventory["files"]),
            "expanded_bytes": inventory["expanded_bytes"],
            "files": inventory["files"],
        },
        "license": {"id": LICENSE, "state": "verified"},
        "entitlement_state": "verified",
        "owner": "platform-pvc",
        "retention": "retained-platform",
    }
    manifest_digest = digest(manifest)
    fixtures = {
        "schema": "fs2-serve.nebius.ai/cosmos-transfer25-semantic-fixtures/v1",
        "source": "https://github.com/NVIDIA/paidf-augmentation/tree/bc5719362492a1e3b40bd7d33b43c46dd89efad5",
        "note": "Public NVIDIA intersection video, explicitly prepared before invocation; no hidden resize/trim.",
        "requests": [
            {
                "id": row["id"],
                "source": row["generation"]["source"],
                "prompt_sha256": row["generation"]["prompt_sha256"],
                "parameters": row["generation"]["request_parameters"],
                "request_sha256": row["generation"]["request_sha256"],
            }
            for row in evidence
        ],
    }
    fixture_bytes = json.dumps(fixtures, indent=2).encode() + b"\n"
    validator_path = "k8s-inference/models/general-media/cosmos-transfer25/qualify_adapter.py"
    fixture_path = "k8s-inference/models/general-media/cosmos-transfer25/semantic-fixtures.json"
    record = {
        "schema": "fs2-serve.nebius.ai/model/v1",
        "model": {
            "id": MODEL,
            "display_name": "NVIDIA Cosmos Transfer 2.5",
            "family": "video-generation",
            "tested_lane": True,
            "source": {
                "kind": "ngc-nim",
                "repository": "nvcr.io/nim/nvidia/cosmos-transfer2.5-2b",
                "revision": revision,
                "license": {
                    "id": LICENSE,
                    "state": "verified",
                    "notes": "Built on NVIDIA Cosmos. The model card specifies the Open Model License; /v1/license "
                    "also supplies the NVIDIA Software License Agreement. This canary is internal evaluation only; "
                    "production and third-party service rights are license-dependent, not conferred by key access.",
                },
                "entitlement": {
                    "required": True,
                    "state": "verified",
                    "credential_contract": "isolated-stockholm-existing-ngc-secret-refs/20260920",
                    "notes": "Exact NIM image and all 194 assets downloaded using existing operator-provisioned "
                    "Secret references. No legacy credential copies or credential values are part of this declaration.",
                },
            },
        },
        "runtime": {
            "kind": "nim",
            "version": "1.1.0-profile-" + PROFILE,
            "image": {"reference": IMAGE, "digest": NIM_DIGEST, "state": "resolved"},
            "command": ["/opt/nvidia/nvidia_entrypoint.sh", "/bin/bash", "-c", "$SERVER_START_SCRIPT_PATH"],
        },
        "resources": {
            "cpu_millis": 8500,
            "memory_bytes": 96 * 1024**3 + 512 * 1024**2,
            "host_ram_min_bytes": None,
            "scaler_owner": "nebius-managed-node-group-autoscaler",
            "gpu": {
                "class": "NVIDIA-H100-SXM5-80GB",
                "count": 1,
                "topology": "single-gpu",
                "placement": None,
                "b300_state": "unverified",
                "alternatives": [],
            },
        },
        "interface": {
            "execution_mode": "http",
            "protocols": ["native"],
            "endpoints": {"native": "/v1/transfer"},
            "readiness": {"method": "GET", "path": "/v1/health/ready", "expected_status": 200, "timeout_seconds": 2700},
            "warmup": None,
            "policy": {
                "operations": ["transfer-video"],
                "license_enforced": True,
                "non_clinical": True,
                "commercial_use": "license-dependent",
            },
            "mcp": {"discoverable": True, "invocable": False},
        },
        "startup": {
            "default": "conventional",
            "fallback": "conventional",
            "enabled_mechanisms": ["conventional"],
            "experiments": [],
            "multi_gpu_criu": "unproven-disabled",
        },
        "cache": {
            "owner": "platform-pvc",
            "shared_path": "/mnt/fs2-serve-cache/models/" + MODEL,
            "local_path": "/var/lib/fs2-serve/cache/models/" + MODEL,
            "pre_pull_image": True,
            "artifact": {
                "state": "platform-verified",
                "kind": "nim-cache",
                "manifest_digest": manifest_digest,
                "expanded_bytes": inventory["expanded_bytes"],
                "minimum_bytes": inventory["expanded_bytes"],
                "capacity_bound_bytes": 96 * 1024**3,
                "staged": False,
            },
        },
        "semantic_validator": {
            "kind": "repository-command",
            "contract": "cosmos-transfer25-edge-weather/v1",
            "source_path": validator_path,
            "source_sha256": hashlib.sha256((HERE / "qualify_adapter.py").read_bytes()).hexdigest(),
            "fixture_path": fixture_path,
            "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "request_count": 2,
            "distinct_requests": True,
            "distinct_responses": True,
        },
        "support": {
            "state": "qualified",
            "route_exposed": False,
            "non_clinical": True,
            "limitations": [
                "Internal isolated evaluation, not customer-ready; public gateway/MCP, chat preview "
                "and approved batch flows are separate gates.",
                "Exactly one H100, this NIM/profile and immutable adapter "
                + ADAPTER
                + " only. No B300, autoscaling or snapshot qualification.",
                "Source MP4 must be 640x480 or 1280x720, constant integer 1–30 FPS, "
                "93–400 frames and at most 128 MiB; silent output.",
                "Native edge-conditioned transfer only. No implicit cropping, trimming, retiming, "
                "depth/segmentation controls or action model.",
                "Automated full-frame motion and sampled-weather checks do not establish physical fidelity "
                "or annotation correctness; human review is required.",
                "Adapter bounds concurrency to one and refuses new work after unknown upstream completion; "
                "no automatic generation retry.",
                "Rain seed 43 preserved frame geometry and passed weather verification but failed motion "
                "(0.649145 < 0.682); it is retained as failed evidence and not a qualified recipe.",
            ],
        },
        "evidence": [
            {
                "classification": "measured-platform",
                "hardware": "NVIDIA H100 80GB HBM3; driver 580.173.02",
                "outcome": "live-qualified",
                "source_commit": SOURCE_COMMIT,
                "summary": "Two distinct native outputs passed full-video alignment and sampled-weather verification through "
                "the exact CPU adapter container connected to the private Stockholm NIM canary. Receipt digest "
                + digest(evidence)
                + ". The rain clip failed the unchanged PAIDF motion threshold and is NOT accepted for a blueprint batch. "
                "This is not public gateway, workbench, managed cache startup, lifecycle or customer acceptance.",
            }
        ],
        "provenance": [
            {
                "commit": SOURCE_COMMIT,
                "tree": SOURCE_TREE,
                "path": "k8s-inference/models/general-media/cosmos-transfer25/selected-profile-20260920.json",
                "classification": "reviewed-input",
            },
            {
                "commit": SOURCE_COMMIT,
                "tree": SOURCE_TREE,
                "path": "k8s-inference/models/general-media/cosmos-transfer25/adapter/app.py",
                "classification": "measured-handoff",
            },
        ],
    }
    native = {
        "schema": "fs2-serve.nebius.ai/native-catalog-model/v1",
        "record": record,
        "variant_id": VARIANT,
        "runtime_architecture": "vendor-nim",
        "semantic_requests": {
            "state": "qualified",
            "blocker": None,
            "serialization": "sha256-canonical-json-no-newline/v1",
            "invocation": {
                "operation": "transfer-video",
                "protocol": "native",
                "method": "POST",
                "endpoint": "/v1/transfer",
            },
            "requests": [{"id": row["id"], "payload_sha256": row["generation"]["request_sha256"]} for row in evidence],
            "assets": [],
        },
        "artifact_manifest": {
            "path": "../deployment-runtimes/artifacts/" + MODEL + "-" + manifest_digest + ".json",
            "sha256": manifest_digest,
        },
    }
    qualification = {
        "model_id": MODEL,
        "variant_id": VARIANT,
        "active_runtime": {
            "model_revision": revision,
            "runtime_image_digest": NIM_DIGEST,
            "service": {"name": MODEL, "namespace": "fs2-models", "port": 8000},
        },
        "runtime_origin": {
            "kind": "nvidia-nim",
            "variant_id": VARIANT,
            "source_kind": "ngc-nim",
            "repository": record["model"]["source"]["repository"],
            "relationship": "exact-model",
            "nim_artifact_parity": "verified",
        },
        "states": {
            "registered": True,
            "route_active": False,
            "runtime_ready": True,
            "semantic_qualified": True,
            "http_mcp_qualified": False,
            "cold_start_qualified": False,
            "elasticity_qualified": False,
        },
        "policy": {"license_id": LICENSE, "non_clinical": True, "commercial_use": "license-dependent"},
        "evidence": {
            "audited_catalog_sha256": digest(record),
            "retained_deployments_sha256": digest(evidence),
            "audited_live_routes_sha256": None,
            "model_discovery_sha256": None,
            "mcp_discovery_sha256": None,
            "http_mcp_acceptance_sha256": None,
            "cold_start_acceptance_sha256": None,
            "elasticity_acceptance_sha256": None,
        },
    }
    return {
        "catalog/runtime/native/" + MODEL + ".json": native,
        "catalog/runtime/deployment-runtimes/" + MODEL + ".json": {
            "schema": "fs2-serve.nebius.ai/deployment-runtime/v1",
            "model_id": MODEL,
            "variant_id": VARIANT,
            "record": record,
            "qualification": qualification,
        },
        "catalog/runtime/deployment-runtimes/artifacts/" + MODEL + "-" + manifest_digest + ".json": manifest,
        "models/general-media/cosmos-transfer25/semantic-fixtures.json": fixtures,
        "models/general-media/cosmos-transfer25/adapter-qualification-20260920.json": evidence,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-directory", type=Path, action="append", required=True)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.result_directory)))
