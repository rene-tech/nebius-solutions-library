#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "pyarrow==21.0.0", "rapidfuzz==3.14.1"]
# ///
"""Matched full-recording medical ASR quality through ordinary public App APIs.

Credentials stay in memory. One request per model, at most two models in flight.
No cloud/model configuration changes, hidden retries, trimming or text repair.
Original scorer: acceptance/nemotron-speech-20260916/score_medical.py.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from rapidfuzz.distance import Levenshtein

HERE = Path(__file__).resolve().parent
SOLUTION = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "nemotron-speech-20260916"))
from score_medical import alignment, reference, words
from public_probe import resolve_result, upload_artifact

MODELS = (
    "parakeet-realtime-eou-120m-v1",
    "nemotron-speech-en-0-6b",
    "nemotron-speech-multilingual-0-6b",
)
PARQUET_SHA = "494a635916ceaed914f6238fb7acf37e38a1e8432c30663a2f6f484dbdec58e0"


def sha(content):
    return hashlib.sha256(content).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def score(expected, actual):
    result = alignment(expected, actual)
    source, target = "".join(words(expected)), "".join(words(actual))
    return result | {"reference_characters": len(source),
                     "character_errors": Levenshtein.distance(source, target),
                     "cer": Levenshtein.distance(source, target) / len(source)}


def prepare_cases(assets, directory, german):
    cases = []
    for number in ("01", "02"):
        source = assets / f"ready/en/day1_consultation{number}_conversation.wav"
        target = directory / f"en-{number}.flac"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                        "-c:a", "flac", str(target)], check=True, timeout=60)
        with wave.open(str(source)) as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
            seconds = audio.getnframes() / 16000
        transcript, provenance = reference(assets, f"en-{number}")
        cases.append({"case": f"en-{number}", "language": "en", "path": target,
                      "audio_seconds": seconds, "source_sha256": sha(source.read_bytes()),
                      "reference": transcript, "reference_provenance": provenance,
                      "media_type": "audio/flac"})
    if german:
        import pyarrow.parquet as pq
        source = assets / "references/de/multimed/test.parquet"
        assert sha(source.read_bytes()) == PARQUET_SHA
        dataset = pq.read_table(source).to_pylist()
        assert len(dataset) == 1091
        # The exact earlier report-quality selection, plus its empty-output challenge.
        for index in sorted({i * (len(dataset) - 1) // 29 for i in range(30)} | {193}):
            item = dataset[index]
            target = directory / f"de-{index:04}.ogg"
            target.write_bytes(item["audio"]["bytes"])
            decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(target),
                                               "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"])
            cases.append({"case": f"de-{index:04}", "language": "de", "path": target,
                          "audio_seconds": len(decoded) / 32000,
                          "source_sha256": sha(target.read_bytes()), "reference": item["text"],
                          "reference_provenance": {"dataset": "leduckhai/MultiMed",
                                                   "revision": "459d0ab6db332904f9d7b76a8baabf3333958fa8",
                                                   "parquet_sha256": PARQUET_SHA, "row": index},
                          "media_type": "audio/ogg"})
    return cases


def upload(client, case, model):
    content = case["path"].read_bytes()
    # Reuse the existing bounded proxy/direct-storage upload implementation.
    if case["media_type"] == "audio/flac":
        row = {"transport_sha256": sha(content), "transport_bytes": len(content)}
        return upload_artifact(client, case["path"], model, row, "medical-asr-" + uuid4().hex)
    response = client.post("/v1/scientific-artifacts/uploads", json={
        "model_id": model, "sha256": sha(content), "size_bytes": len(content),
        "media_type": case["media_type"], "compression": "none",
    }, headers={"idempotency-key": "medical-asr-upload-" + uuid4().hex})
    response.raise_for_status()
    handle = response.json()
    assert len(content) <= handle["max_content_bytes"]
    response = client.put(handle["content_path"], content=content,
                          headers={"content-type": case["media_type"]})
    response.raise_for_status()
    response = client.post("/v1/scientific-artifacts/uploads/" + handle["upload_id"] + ":finalize",
                           json={"operation_id": handle["operation_id"]})
    response.raise_for_status()
    return response.json()


def one_request(client, model, case, artifact, repetition, out):
    row = {key: value for key, value in case.items() if key != "path"}
    row.update(model=model, repetition=repetition, status="started", started_at=datetime.now(UTC).isoformat())
    stem = f"{model}-{case['case']}-r{repetition}"
    save(out / f"{stem}.json", row)
    started = time.monotonic()
    try:
        payload = {"audio": artifact}
        if model != MODELS[0]:
            payload["options"] = {"model": model.replace("0-6b", "0.6b"),
                                   "language": case["language"], "output_granularity": "word"}
        response = client.post("/v1/models/" + model + ":invoke",
                               json={"operation": "transcribe", "payload": payload},
                               headers={"idempotency-key": "medical-asr-" + uuid4().hex,
                                        "x-fs2-wait-seconds": "0"})
        row["submit_status"] = response.status_code
        response.raise_for_status()
        value = response.json()
        if response.status_code == 202:
            operation_id = value.get("id") or value["operation_id"]
            row["operation_id"] = operation_id
            save(out / f"{stem}.json", row)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                response = client.get("/v1/operations/" + operation_id)
                response.raise_for_status()
                operation = response.json()
                if operation["status"] == "succeeded":
                    row["operation"] = operation
                    response = client.get("/v1/operations/" + operation_id + "/result")
                    response.raise_for_status()
                    value = response.json()
                    break
                if operation["status"] in {"failed", "cancelled", "expired", "preempted"}:
                    row["operation"] = operation
                    raise RuntimeError("operation_" + operation["status"])
                time.sleep(0.5)
            else:
                row["cancel_status"] = client.post("/v1/operations/" + operation_id + ":cancel").status_code
                raise RuntimeError("operation_deadline")
        result = resolve_result(client, value, row)
        row["wall_seconds"] = time.monotonic() - started
        if not isinstance(result.get("text"), str):
            raise RuntimeError("missing_transcript")
        row["text"] = result["text"]
        row["result_audio_seconds"] = result.get("audio_seconds")
        row["processing_seconds"] = result.get("processing_seconds")
        row["result_keys"] = sorted(result)
        row["full_duration"] = abs(result.get("audio_seconds", -1) - case["audio_seconds"]) < 0.002
        row["raw_result_sha256"] = sha(json.dumps(result, sort_keys=True).encode())
        with gzip.open(out / f"{stem}.result.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
        if not row["full_duration"]:
            raise RuntimeError("audio_duration_mismatch")
        row["status"] = "succeeded" if row["text"].strip() else "empty-transcript"
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
        row.update(status="failed", error=type(error).__name__, wall_seconds=time.monotonic() - started)
        if isinstance(error, httpx.HTTPStatusError):
            row["error_http_status"] = error.response.status_code
            # Error codes only; never signed URLs, headers or exception text.
            try:
                row["error_code"] = error.response.json().get("error", {}).get("code")
            except (ValueError, AttributeError):
                pass
        elif isinstance(error, RuntimeError):
            row["error_code"] = str(error)
    row["quality"] = score(case["reference"], row.get("text", ""))
    row["finished_at"] = datetime.now(UTC).isoformat()
    save(out / f"{stem}.json", row)
    print(json.dumps({key: row.get(key) for key in ("model", "case", "repetition", "status", "wall_seconds", "error_code")}
                     | {"wer": row["quality"]["wer"]}), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="fs2-storage-h100")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--german-subset", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3, choices=(1, 3))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    raw = json.loads(subprocess.check_output(kube + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]))
    token = base64.b64decode(raw["data"]["token"]).decode().strip()
    run = {"started_at": datetime.now(UTC).isoformat(), "scope": "public API transcription quality, not clinical validation or cold-start qualification",
           "models": MODELS, "repetitions": args.repetitions, "max_concurrency": 2,
           "warmup": "existing resident production models; no extra warmup or restart; all first requests retained",
           "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip(),
           "driver_sha256": sha(Path(__file__).read_bytes()), "key_revoked": False}
    pods = json.loads(subprocess.check_output(kube + ["-n", "fs2-models", "get", "pods", "-o", "json"]))
    run["workers_before"] = [{"pod": item["metadata"]["name"], "uid": item["metadata"]["uid"],
                              "node": item["spec"]["nodeName"], "images": [c["image"] for c in item["spec"]["containers"]],
                              "container_statuses": item["status"].get("containerStatuses", [])}
                             for item in pods["items"] if any(item["metadata"]["name"].startswith(m) for m in MODELS)]
    run["pinned_models"] = {m: json.loads((SOLUTION / "catalog/runtime/native" / f"{m}.json").read_text())["record"]["model"]["source"] for m in MODELS}
    key_id = None
    with tempfile.TemporaryDirectory(prefix="fs2-medical-asr-") as directory, httpx.Client(
        base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False,
    ) as admin:
        try:
            admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}).raise_for_status()
            response = admin.post("/admin/api/v1/keys", json={
                "name": "medical-asr-comparison-" + uuid4().hex[:10], "tenant_id": "rene", "principal_id": "rene",
                "models": list(MODELS), "scopes": ["catalog.read", "inference.invoke", "operations.read",
                    "operations.result", "operations.cancel", "artifacts.write", "use.nonclinical"],
                "max_concurrency": 2, "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            })
            response.raise_for_status()
            disclosure = response.json()["data"]
            key_id, key = disclosure["key"]["id"], disclosure["secret"]
            run["temporary_key_id"] = key_id
            save(args.output / "run.json", run)
            cases = prepare_cases(args.assets, Path(directory), args.german_subset)
            run["cases"] = [{k: v for k, v in case.items() if k != "path"} for case in cases]
            save(args.output / "run.json", run)

            def cohort(model):
                with httpx.Client(base_url=args.origin, headers={"authorization": "Bearer " + key},
                                  timeout=120, trust_env=False) as client:
                    selected = [case for case in cases if case["language"] == "en" or model == MODELS[2]]
                    rows = []
                    for case in selected:
                        artifact = upload(client, case, model)
                        for repetition in range(1, (args.repetitions if case["language"] == "en" else 1) + 1):
                            rows.append(one_request(client, model, case, artifact, repetition, args.output))
                    return rows

            with ThreadPoolExecutor(max_workers=2) as pool:
                run["cohorts"] = [len(rows) for rows in pool.map(cohort, MODELS)]
        finally:
            if key_id:
                run["key_revoked"] = admin.delete("/admin/api/v1/keys/" + key_id).status_code == 200
            run["finished_at"] = datetime.now(UTC).isoformat()
            save(args.output / "run.json", run)
            admin.delete("/admin/api/v1/session")
    print(json.dumps({"finished": True, "key_revoked": run["key_revoked"], "cohorts": run.get("cohorts")}), flush=True)


if __name__ == "__main__":
    main()
