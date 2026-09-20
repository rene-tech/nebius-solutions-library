"""Public-file benchmarks for speech, biological age, segmentation and video.

Fixtures are immutable artifact descriptors. Model-specific validators retain
quality observations separately from transport/format success.
"""

import base64
import hashlib
import importlib.util
import io
import json
import math
import re
import sys
import subprocess
import tempfile
import wave
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


MEDIA_FIXTURES = module("fixtures", ROOT / "acceptance/wan2-sam2-20260920/fixtures.py")
MEDIA = module("benchmark_media_validation", ROOT / "acceptance/wan2-sam2-20260920/qualify.py")
VISUAL = module("benchmark_visual_validation", ROOT / "acceptance/visual-science-20260919/run_public.py")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def artifact_bytes(client, ref):
    artifact_id = str(UUID(ref["artifact_id"]))
    response = client.get(f"/v1/artifacts/{artifact_id}/content")
    response.raise_for_status()
    raw = response.content
    if len(raw) != ref["size_bytes"] or digest(raw) != ref["sha256"]:
        raise RuntimeError("result_artifact_identity_mismatch")
    return raw


def result_bytes(client, path):
    raw = path.read_bytes()
    if raw.startswith(b"{"):
        value = json.loads(raw)
        if value.get("schema") == "fs2-serve.nebius.ai/operation-artifact-result/v1":
            return artifact_bytes(client, value["artifact"])
    return raw


def word_error_rate(reference, hypothesis):
    """Unit-cost word Levenshtein; report exact normalization with the result."""
    ref, hyp = (re.findall(r"\w+", text.casefold()) for text in (reference, hypothesis))
    if not ref:
        return None
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        current = [i]
        for j, other in enumerate(hyp, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(ref)


def validate(model, request, oracle, raw, directory, index):
    if model == "cellpose-cpsam-v2":
        return VISUAL.validate_cellpose(raw, oracle["input_sha256"], directory, f"cellpose-{index}")
    if model == "scvi-scanvi":
        return VISUAL.validate_scvi(raw, oracle["input_sha256"], request["method"], directory, f"scvi-{index}")
    if model == "sam2-1-hiera-large":
        return MEDIA.validate_sam(raw, request["mode"])
    if model in {"wan2-2-t2v-nim", "wan2-2-i2v-nim"}:
        probe = MEDIA.probe_mp4(raw)
        stream = probe["streams"][0]
        if (stream["width"], stream["height"]) != (832, 480) or not 3.5 <= float(probe["format"]["duration"]) <= 4.5:
            raise RuntimeError("video_dimensions_or_duration_mismatch")
        return {"sha256": digest(raw), "probe": probe, "scope": "decodable-video-format-not-scientific-accuracy"}
    if model == "cosmos-transfer2-5-2b":
        probe = MEDIA.probe_mp4(raw)
        stream = probe["streams"][0]
        actual = [stream["width"], stream["height"], int(stream["nb_frames"]), stream["avg_frame_rate"]]
        if actual != oracle["geometry_frames_rate"]:
            raise RuntimeError("transfer_geometry_or_timing_mismatch")
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            video.write(raw)
            video.flush()
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", video.name, "-f", "null", "-"],
                           check=True, capture_output=True, timeout=90)
        return {"sha256": digest(raw), "probe": probe, "all_frames_decoded": True,
                "scope": "geometry-timing-artifact-integrity-not-physical-or-weather-quality"}
    if model == "magpie-tts-multilingual-357m":
        with wave.open(io.BytesIO(raw)) as audio:
            frames = audio.readframes(audio.getnframes())
            seconds = audio.getnframes() / audio.getframerate()
            if audio.getframerate() != 22050 or audio.getsampwidth() != 2 or seconds < 1:
                raise RuntimeError("tts_wave_contract_mismatch")
        energy = sum(int.from_bytes(frames[i:i + 2], "little", signed=True) ** 2 for i in range(0, len(frames), 2))
        if energy == 0:
            raise RuntimeError("tts_silent_wave")
        return {"sha256": digest(raw), "audio_seconds": seconds, "rms": math.sqrt(energy / (len(frames) / 2)),
                "scope": "complete-nonsilent-wave-not-transcription-accuracy"}
    value = json.loads(raw)
    if model == "altumage":
        if value.get("model_id") != model or value.get("sample_count") != len(request["samples"]):
            raise RuntimeError("altumage_identity_mismatch")
        predictions = value["predictions"]
        for actual, sample in zip(predictions, request["samples"], strict=True):
            if actual["sample_id"] != sample["sample_id"] or not math.isfinite(actual["predicted_chronological_age_years"]):
                raise RuntimeError("altumage_prediction_invalid")
        return {"predictions": predictions, "scope": "synthetic-input-shape-and-finite-results-not-age-accuracy"}
    if model in {"nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b", "parakeet-realtime-eou-120m-v1",
                 "diar-streaming-sortformer-4spk-v2-1"}:
        seconds = value.get("audio_seconds")
        if seconds is None or abs(seconds - oracle["audio_seconds"]) > 1:
            raise RuntimeError("speech_audio_duration_mismatch")
        if model.startswith("diar-"):
            activity = [event for event in value.get("events", []) if event.get("type") == "speaker.activity"]
            if not activity:
                raise RuntimeError("diarization_activity_missing")
            for event in activity:
                if not event["probabilities"] or any(not math.isfinite(p) or not 0 <= p <= 1
                    for frame in event["probabilities"] for p in frame):
                    raise RuntimeError("diarization_probabilities_invalid")
            return {"audio_seconds": seconds, "activity_events": len(activity), "scope": "activity-contract-not-speaker-ground-truth"}
        text = value.get("text", "").strip()
        if len(text.split()) < 20:
            raise RuntimeError("speech_transcript_incomplete")
        return {"audio_seconds": seconds, "processing_seconds": value.get("processing_seconds"),
                "words": len(text.split()), "transcript_sha256": digest(text.encode()),
                "word_error_rate": word_error_rate(oracle.get("reference_text", ""), text),
                "wer_normalization": "unicode-word-tokens-casefold-no-number-expansion",
                "scope": "full-recording-transcription-quality-observation-not-clinical-validation"}
    raise RuntimeError("extra_semantic_validator_unavailable")


def execute(trial, client, directory, invoke, timeout):
    case = trial["case_spec"]
    artifact_id = str(UUID(case["fixture_ref"].removeprefix("artifact://")))
    response = client.get(f"/v1/artifacts/{artifact_id}/content")
    response.raise_for_status()
    if digest(response.content) != case["fixture_sha256"]:
        raise RuntimeError("fixture_digest_changed")
    fixture = response.json()
    if fixture["model_id"] != case["model_id"] or not 1 <= len(fixture["requests"]) <= 4:
        raise RuntimeError("fixture_model_or_count_mismatch")
    calls, semantics = [], []
    for index, record in enumerate(fixture["requests"]):
        call = invoke(client, case["model_id"], "native", fixture["operation"], record["payload"],
                      f"benchmark-{trial['id']}-{index}", directory, timeout)
        semantics.append(validate(case["model_id"], record["payload"], record["oracle"],
                                  result_bytes(client, call[0]), directory, index))
        calls.append(call)
    return calls, {"status": "PASS", "requests": semantics, "fixture_attribution": fixture.get("attribution")}
