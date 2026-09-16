"""Generate additive catalog declarations only from passed native GPU receipts."""

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPECS = {
    "parakeet": (
        "parakeet-realtime-eou-120m-v1",
        "Parakeet Realtime EOU 120M",
        "speech-recognition",
        "transcribe",
        "parakeet_realtime_eou_120m-v1",
        "a7e2b4629593dce0ec19f600e00e9904353fda2d",
        460062720,
        "6603a22a53b7c1a4bac4736cb24628fb568a7102ba931a28c799e2e72f109893",
    ),
    "magpie": (
        "magpie-tts-multilingual-357m",
        "Magpie Multilingual TTS 357M",
        "speech-synthesis",
        "synthesize",
        "magpie_tts_multilingual_357m",
        "19806879b16d3f2ccf28fb112b1bcd16a3c7923e",
        1470208000,
        "ec675fa8c02b9c1d5382c5c2b5a6acec6492c1e8344866c07cf3892185d18953",
    ),
    "sortformer": (
        "diar-streaming-sortformer-4spk-v2-1",
        "Sortformer Streaming Four Speaker 2.1",
        "speaker-diarization",
        "diarize",
        "diar_streaming_sortformer_4spk-v2.1",
        "fafaab5faa1617a0ca52d38dd3dc4bd636800d3d",
        471367680,
        "8abd32832159c6ac1148c926b7276f35ba34582c444e559dce1f1253fea42ef8",
    ),
}


def digest(value):
    return hashlib.sha256(
        (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode()
    ).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main(args):
    receipts = json.loads(Path(args.evidence).read_text())
    if receipts.get("status") != "passed":
        raise ValueError("native GPU acceptance must pass before catalog generation")
    evidence_sha = hashlib.sha256(Path(args.evidence).read_bytes()).hexdigest()
    source_commit = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    )
    source_tree = (
        subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT)
        .decode()
        .strip()
    )
    source_path = "k8s-inference/acceptance/voice-agent-20260916/native_probe.py"
    source = ROOT.parent / source_path
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    packaged = ROOT / "catalog/runtime/packaged-repository" / source_path
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(source.read_bytes())
    base = json.loads(
        (ROOT / "catalog/runtime/native/nemotron-speech-en-0-6b.json").read_text()
    )
    profiles = json.loads((ROOT / "catalog/profiles/model-profiles.json").read_text())
    compatibility = json.loads(
        (ROOT / "catalog/profiles/model-accelerator-compatibility.json").read_text()
    )
    adapters_path = (
        ROOT
        / "components/control-plane/src/fs2_serve/model_input_schemas/runtime-adapters.json"
    )
    adapters = json.loads(adapters_path.read_text())
    spec = importlib.util.spec_from_file_location(
        "voice_render", Path(__file__).with_name("render.py")
    )
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    ids, paths = [], []
    for short, (
        model,
        display,
        family,
        operation,
        repository,
        revision,
        size,
        weightsha,
    ) in SPECS.items():
        rows = receipts["models"][model]
        if (
            len(rows) != 2
            or len({r["request_payload_sha256"] for r in rows}) != 2
            or len({r["response_sha256"] for r in rows}) != 2
        ):
            raise ValueError("two distinct measured native requests/responses required")
        image = getattr(args, short + "_image")
        imagedigest = image.split("@", 1)[1]
        variant = model + "-nemo-cuda-v1"
        files = sorted(
            [
                {"path": repository + ".nemo", "bytes": size, "sha256": weightsha},
                {
                    "path": "nanocodec.nemo",
                    "bytes": 425021440,
                    "sha256": "28c2518de3e3d5a2c7d9bca40a7ebc0644eb76c60b890970365325bdd8e9f099",
                },
            ],
            key=lambda x: x["path"],
        )
        manifest = {
            "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
            "model_id": model,
            "kind": "weights",
            "source": {
                "uri": f"https://huggingface.co/nvidia/{repository}/tree/{revision}",
                "revision": revision,
            },
            "content": {
                "digest": digest(files),
                "expanded_bytes": size + 425021440,
                "files": files,
            },
            "license": {"id": "NVIDIA-Open-Model-License", "state": "verified"},
            "entitlement_state": "not-required",
            "owner": "runtime-image",
            "retention": "retained-platform",
        }
        artifactsha = digest(manifest)
        artifactpath = f"artifacts/{model}-{artifactsha}.json"
        save(ROOT / "catalog/runtime/deployment-runtimes" / artifactpath, manifest)
        record = copy.deepcopy(base["record"])
        record["model"].update(id=model, display_name=display, family=family)
        record["model"]["source"].update(
            repository="nvidia/" + repository, revision=revision
        )
        record["model"]["source"]["license"]["notes"] = (
            "Pinned official NVIDIA Open Model License; see docs/voice-agent-onboarding-20260916.md. Non-clinical only."
        )
        record["runtime"] = {
            "kind": "custom",
            "version": "nemo-3d91009e-voice-20260916",
            "image": {"reference": image, "digest": imagedigest, "state": "resolved"},
            "command": ["fs2-voice-serve"],
        }
        record["resources"]["gpu"]["class"] = "NVIDIA-L40S-48GB"
        record["interface"]["policy"]["operations"] = [operation]
        record["interface"]["readiness"]["timeout_seconds"] = 900
        record["startup"]["experiments"][0]["reason"] = (
            "No restored voice-runtime public-path cohort exists. Conventional pinned-image/weight cache only; CUDA/CRIU snapshots remain disabled."
        )
        record["cache"]["shared_path"] = "/mnt/fs2-serve-cache/models/" + model
        record["cache"]["local_path"] = "/var/lib/fs2-serve/cache/models/" + model
        record["cache"]["artifact"].update(
            manifest_digest=artifactsha,
            expanded_bytes=size + 425021440,
            minimum_bytes=size + 425021440,
            capacity_bound_bytes=size + 425021440,
        )
        record["semantic_validator"] = {
            "kind": "repository-command",
            "contract": "voice-native-two-distinct-requests/v1",
            "source_path": source_path,
            "source_sha256": source_sha,
            "fixture_path": source_path,
            "fixture_sha256": source_sha,
            "request_count": 2,
            "distinct_requests": True,
            "distinct_responses": True,
        }
        record["support"]["limitations"] = [
            "Independent pinned NVIDIA NeMo runtime, not NVIDIA NIM. One active GPU request/session per replica; overload retries only before input/audio acceptance.",
            "Only L40S single-GPU resident workers are hardware qualified here. No public-path or snapshot qualification is implied by these native receipts.",
            "Magpie uses bounded phrase-incremental synthesis, not codec-token streaming; named voices are actual v2607 speakers. Parakeet is English only. Sortformer labels are anonymous arrival-order speakers, not identities.",
            "Synthetic clean/noisy fixtures are protocol and latency evidence, not medical accuracy or clinical validation. Abrupt worker loss explicitly fails established sessions; reconnect starts a new operation.",
        ]
        record["evidence"] = [
            {
                "classification": "measured-platform",
                "hardware": "NVIDIA L40S 48GB / SM89 / driver580.173.02",
                "outcome": "live-qualified",
                "source_commit": source_commit,
                "summary": f"Two distinct private native requests passed. acceptance/voice-agent-20260916/native-results.json SHA256 {evidence_sha}. Streaming/noisy/lifecycle measurements documented separately; no static route grant.",
            }
        ]
        record["provenance"] = [
            {
                "commit": source_commit,
                "tree": source_tree,
                "path": "k8s-inference/components/voice-runtime",
                "classification": "measured-handoff",
            },
            {
                "url": f"https://huggingface.co/nvidia/{repository}/tree/{revision}",
                "revision": revision,
                "classification": "reviewed-input",
            },
        ]
        declaration = {
            "schema": base["schema"],
            "record": record,
            "variant_id": variant,
            "runtime_architecture": "cuda",
            "semantic_requests": {
                "state": "qualified",
                "blocker": None,
                "serialization": "sha256-json-compact-no-newline/v1",
                "invocation": {
                    "operation": operation,
                    "protocol": "native",
                    "method": "POST",
                    "endpoint": "/generate",
                },
                "requests": [
                    {"id": r["id"], "payload_sha256": r["request_payload_sha256"]}
                    for r in rows
                ],
                "assets": [],
            },
            "artifact_manifest": {
                "path": "../deployment-runtimes/" + artifactpath,
                "sha256": artifactsha,
            },
        }
        save(ROOT / f"catalog/runtime/native/{model}.json", declaration)
        qualification = {
            "model_id": model,
            "variant_id": variant,
            "active_runtime": {
                "model_revision": revision,
                "runtime_image_digest": imagedigest,
                "service": {
                    "name": f"fs2-voice-{short}-r20260916",
                    "namespace": "fs2-models",
                    "port": 8000,
                },
            },
            "runtime_origin": {
                "kind": "independent-runtime",
                "variant_id": variant,
                "source_kind": "huggingface",
                "repository": "nvidia/" + repository,
                "relationship": "exact-model",
                "nim_artifact_parity": "not-applicable",
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
            "policy": {
                "license_id": "NVIDIA-Open-Model-License",
                "non_clinical": True,
                "commercial_use": "license-dependent",
            },
            "evidence": {
                "audited_catalog_sha256": digest(record),
                "audited_live_routes_sha256": None,
                "cold_start_acceptance_sha256": None,
                "elasticity_acceptance_sha256": None,
                "http_mcp_acceptance_sha256": None,
                "mcp_discovery_sha256": None,
                "model_discovery_sha256": None,
                "retained_deployments_sha256": evidence_sha,
            },
        }
        save(
            ROOT / f"catalog/runtime/deployment-runtimes/{model}.json",
            {
                "schema": "fs2-serve.nebius.ai/deployment-runtime/v1",
                "model_id": model,
                "variant_id": variant,
                "record": record,
                "qualification": qualification,
            },
        )
        manifestpath = f"models/voice-agent/k8s/{model}.yaml"
        rendered = renderer.render(
            model=model,
            image=image,
            namespace="fs2-models",
            name=model,
            replicas=0,
            artifact_config="fs2-speech-runtime-settings",
        )
        save(ROOT / manifestpath, rendered)
        profiles["managed_native_model_ids"] = list(
            dict.fromkeys(profiles["managed_native_model_ids"] + [model])
        )
        profiles["workload_placements"][model] = {
            "model_id": model,
            "runtime_variant": "deployment/" + model,
            "state": "fixture-only",
            "gpu_request": 1,
            "workload_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "selection_mode": "accelerator-class",
            "compatible_pool_ids": ["unbound-l40s"],
            "required_node_labels": {
                "accelerator.fs2.nebius/class": "nvidia-l40s-48gb"
            },
        }
        profiles["model_autoscaling_targets"][model] = {
            "deployment": model,
            "gpu_count": 1,
            "ephemeral_storage_request_gib": 16,
        }
        profiles["model_artifacts"][model] = {
            "manifest_paths": [manifestpath],
            "keeper_paths": [],
            "required_secrets": [],
        }
        compatibility["models"][model] = {
            "runtimes": {
                variant: {
                    "runtime_variant": variant,
                    "runtime_ref": image,
                    "runtime_ref_kind": "container-image-digest",
                    "requirements": {
                        "gpu_count": 1,
                        "topology": "single-gpu",
                        "host_architectures": ["amd64"],
                    },
                    "bindings": [
                        {
                            "accelerator_class": "nvidia-l40s-48gb",
                            "pool_template": "unbound-l40s",
                            "state": "hardware-validated",
                            "enabled": True,
                            "gpu_count": 1,
                            "catalog_b300_state": None,
                            "evidence": "acceptance/voice-agent-20260916/native-results.json@sha256:"
                            + evidence_sha,
                        }
                    ],
                    "qualification_candidates": [],
                }
            }
        }
        adapters[model] = {
            "variant_id": variant,
            "runtime_kind": "custom",
            "source": f"k8s-inference/catalog/runtime/deployment-runtimes/{model}.json",
        }
        ids.append(model)
        paths.append(manifestpath)
    profiles["profiles"]["voice-agent"] = {
        "canonical_routes": ids,
        "manifest_paths": paths,
        "keeper_paths": [],
    }
    save(ROOT / "catalog/profiles/model-profiles.json", profiles)
    save(ROOT / "catalog/profiles/model-accelerator-compatibility.json", compatibility)
    save(adapters_path, adapters)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    for short in SPECS:
        parser.add_argument("--" + short + "-image", required=True)
    main(parser.parse_args())
