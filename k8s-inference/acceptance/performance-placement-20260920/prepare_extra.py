"""Freeze additional public/synthetic fixture artifacts without submitting GPU work."""

import argparse
import json
import os
import time
import wave
from pathlib import Path

import httpx

from runner import ROOT, PUBLIC, canonical, module, sha
from extra import MEDIA_FIXTURES

SPEECH = module("benchmark_speech_upload", ROOT / "acceptance/nemotron-speech-20260916/public_probe.py")


def upload(client, model, raw, media_type):
    identity = {"model_id": model, "sha256": sha(raw), "size_bytes": len(raw), "media_type": media_type, "compression": "none"}
    deadline = time.monotonic() + 3600
    while True:
        response = client.post("/v1/scientific-artifacts/uploads", json=identity,
                               headers={"Idempotency-Key": f"benchmark-fixture-{model}-{sha(raw)}"})
        if response.status_code != 429 or time.monotonic() >= deadline:
            break
        print(json.dumps({"model": model, "fixture_wait": "benchmark_key_concurrency", "retry_seconds": 15}), flush=True)
        time.sleep(15)
    response.raise_for_status()
    reserved = response.json()
    if len(raw) <= reserved["max_content_bytes"]:
        response = client.put(reserved["content_path"], content=raw,
                              headers={"Content-Type": media_type, "Content-Length": str(len(raw))})
    else:
        # No platform Authorization header is forwarded to an object-store URL.
        with httpx.Client(timeout=180, trust_env=False) as storage:
            response = storage.put(reserved["handle"]["url"], content=raw, headers=reserved["handle"]["headers"])
    if response.is_error:
        raise RuntimeError("fixture_upload_failed")
    response = client.post(f"/v1/scientific-artifacts/uploads/{reserved['upload_id']}:finalize",
                           json={"operation_id": reserved["operation_id"]})
    response.raise_for_status()
    artifact = response.json()
    if artifact["sha256"] != sha(raw) or artifact["size_bytes"] != len(raw):
        raise RuntimeError("fixture_finalize_identity_mismatch")
    return artifact


def prepare(args, client):
    cases = []

    def fixture(model, operation, records, workload, attribution="Deterministic synthetic benchmark input; no patient data"):
        descriptor = canonical({"model_id": model, "operation": operation, "requests": records, "attribution": attribution})
        artifact = upload(client, model, descriptor, "application/json")
        cases.append({"case_id": model, "model_id": model, "workload_class": workload,
                      "adapter": "artifact-native-v1", "fixture_sha256": sha(descriptor),
                      "fixture_ref": "artifact://" + artifact["artifact_id"], "repetitions": 3,
                      "cache_condition": "uncontrolled"})
        (args.directory / "cases.json").write_bytes(canonical(cases))
        print(json.dumps({"model": model, "fixture_prepared": True}), flush=True)

    scaling = json.loads((args.aging_reference / "preprocessing.json").read_bytes())
    payload = {"cpg_sites": scaling["cpgs"], "missing_values": "error", "samples": [
        {"sample_id": f"synthetic-dnam-{index}", "beta_values": [max(0,min(1,x + .005 * index)) for x in scaling["center"]]}
        for index in range(2)]}
    fixture("altumage", "predict-age", [{"payload": payload, "oracle": {}}], "two-synthetic-methylomes-20318-cpg")

    for model, operation in (("cellpose-cpsam-v2", "segment-cells"), ("scvi-scanvi", "fit-transform")):
        records = []
        for index in range(2):
            path = args.visual_fixtures / (f"microscopy-{index}.png" if model.startswith("cellpose") else f"cells-{index}.h5ad")
            raw = path.read_bytes()
            artifact = upload(client, model, raw, "image/png" if model.startswith("cellpose") else "application/x-hdf5")
            if model.startswith("cellpose"):
                payload = {"image_base64": artifact, "media_type": "image/png", "diameter": None, "research_only": True}
            else:
                payload = {"anndata_base64": artifact, "filename": path.name,
                           "method": "scvi" if index == 0 else "scanvi", "batch_key": "batch",
                           "labels_key": None if index == 0 else "cell_type", "unlabeled_category": "Unknown",
                           "max_epochs": 2, "n_latent": 4, "seed": 17 + index, "research_only": True}
            records.append({"payload": payload, "oracle": {"input_sha256": sha(raw)}})
        fixture(model, operation, records, "two-synthetic-visual-science-inputs")

    for model, operation, records in (
        ("sam2-1-hiera-large", "segment-track-media", MEDIA_FIXTURES.sam_requests()),
        ("wan2-2-t2v-nim", "generate-video", MEDIA_FIXTURES.wan_t2v_requests()),
        ("wan2-2-i2v-nim", "generate-video", MEDIA_FIXTURES.wan_i2v_requests()),
    ):
        fixture(model, operation, [{"payload": p, "oracle": {}} for _, p in records], "two-deterministic-media-requests")

    fixture("magpie-tts-multilingual-357m", "synthesize", [{"payload": {
        "text": text, "voice": voice, "language": "en"}, "oracle": {}} for text, voice in (
        ("Please meet us at the observatory. The final word is telescope.", "Sofia"),
        ("This workshop tests a different voice. The final word is microscope.", "Jason"))], "two-distinct-voice-syntheses")

    source = args.audio_assets / "ready/en/day1_consultation01_conversation.wav"
    with wave.open(str(source)) as audio:
        seconds = audio.getnframes() / audio.getframerate()
    raw = source.read_bytes()
    reference = args.english_reference.read_text()
    attribution = ("Babylon Health / PriMock57, CC BY 4.0; commit cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5. "
                   "Full doctor/patient role-play tracks mixed at 0.5 gain each and resampled to 16 kHz mono; no cuts.")
    for model in ("nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b",
                  "parakeet-realtime-eou-120m-v1", "diar-streaming-sortformer-4spk-v2-1"):
        artifact = upload(client, model, raw, "audio/wav")
        payload = {"audio": artifact}
        if model.startswith("nemotron"):
            payload["options"] = {"model": model.replace("0-6b", "0.6b"), "language": "en"}
        fixture(model, "diarize" if model.startswith("diar-") else "transcribe",
                [{"payload": payload, "oracle": {"audio_seconds": seconds, "reference_text": reference}}],
                "full-primock57-en-consultation01-458-seconds", attribution)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    for name in ("directory", "token-file", "aging-reference", "visual-fixtures", "audio-assets", "english-reference"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=args.origin, headers={"Authorization": "Bearer " + args.token_file.read_text().strip()},
                      timeout=180, trust_env=False) as client:
        prepare(args, client)


if __name__ == "__main__":
    main()
