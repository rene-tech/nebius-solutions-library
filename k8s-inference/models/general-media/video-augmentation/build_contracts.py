"""Generate checked-in API schema and an unrouted candidate profile from source."""
# ruff: noqa: E402

import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE / "runtime"))
from fs2_video import MODEL_ID, PAIDF_REVISION
from fs2_video.contracts import AugmentationRequest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    schema = AugmentationRequest.model_json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://fs2-serve.nebius.ai/schema/video-augmentation-request/v1",
        }
    )
    write(
        ROOT / "catalog/runtime/schema/video-augmentation-request.schema.json", schema
    )
    write(
        ROOT
        / "components/control-plane/src/fs2_serve/model_input_schemas/video-augmentation.json",
        schema,
    )
    profile = copy.deepcopy(
        json.loads(
            (
                HERE.parent / "lerobot-augmentation/activation/workload-profile.json"
            ).read_text()
        )["profile"]
    )
    profile.update(
        model_id=MODEL_ID,
        display_name="NVIDIA Physical AI video weather augmentation",
        state="candidate-unqualified",
        route_exposed=False,
    )
    profile["source"] = {
        "kind": "git",
        "repository": "NVIDIA/paidf-augmentation",
        "revision": PAIDF_REVISION,
        "review_url": f"https://github.com/NVIDIA/paidf-augmentation/tree/{PAIDF_REVISION}",
        "classification": "candidate-input",
    }
    profile["interface"].update(
        submit_endpoint=f"/v1/models/{MODEL_ID}:submit",
        parameter_schema="fs2-serve.nebius.ai/video-augmentation-request/v1",
        operations=["augment-videos"],
    )
    profile["interface"]["mcp"] = {
        "discoverable": True,
        "invocable": False,
        "tool_name": "submit_physical_ai_video_augmentation",
        "description": "NVIDIA PAIDF: caption, prompt synthesis, full-sequence Cosmos transfer and weather/motion checks. Preview one MP4; batch an approved immutable recipe. Automated checks are not proof of physical fidelity.",
    }
    profile["workload"]["stages"][0]["id"] = "augment-videos"
    profile["semantic_validation"] = {
        "validator_id": "paidf-video-v1",
        "state": "candidate-unqualified",
    }
    profile.pop("qualification", None)
    profile["policy"]["limitations"] = [
        "Candidate, not released or customer-qualified. No live end-to-end or GPU fidelity evidence yet.",
        "Cosmos3-Nano transfer backend, not a claim of Cosmos Transfer2.5 equivalence. NVIDIA PAIDF algorithms with platform transport and five-frame VLM sampling.",
        "MP4 only; 640x480 or1280x720;16..400 frames; constant integer1..30FPS;128MiB per clip;64 clips/2GiB total. No silent trim, resize or chunk/stitch.",
        "Weather presets: overcast, clear, rain. Automated all-frame motion and five-frame weather checks require human review before batch approval. Physical fidelity and training labels are unproven.",
        "Input and output retain their geometry, frame count and rate. Source audio is optionally remuxed unchanged. Originals are never overwritten.",
        "Operator-configured paid VLM/LLM provider and a namespace-local provider secret are required. Provider usage is additional to attributed Cosmos compute.",
    ]

    def compact(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    runtime = b"".join(
        path.read_bytes() for path in sorted((HERE / "runtime/fs2_video").glob("*.py"))
    )
    profile["execution_identity"] = {
        "model_revision": PAIDF_REVISION,
        "runtime_image_digest": None,
        "runtime_recipe_sha256": hashlib.sha256(runtime).hexdigest(),
        "workload_recipe_sha256": hashlib.sha256(
            compact(profile["workload"])
        ).hexdigest(),
        "artifact_manifest_digest": None,
        "execution_identity_sha256": None,
    }
    write(
        HERE / "activation/workload-profile.json",
        {
            "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
            "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
            "profile": profile,
        },
    )


if __name__ == "__main__":
    main()
