#!/usr/bin/env python3
"""Sequential ordinary-key voice acceptance; original public acted sources only."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import wave

MAGPIE = "magpie-tts-multilingual-357m"
SORTFORMER = "diar-streaming-sortformer-4spk-v2-1"
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "preempted"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def intervals(path):
    """Praat long TextGrid, including nonverbal annotated speech intervals."""
    text = path.read_text()
    pattern = r'intervals \[\d+\]:\s*xmin = ([\d.]+)\s*xmax = ([\d.]+)\s*text = "((?:[^"]|"")*)"'
    return [(float(a), float(b), t.replace('""', '"')) for a, b, t in re.findall(pattern, text) if t.strip()]


def diarization_score(value, references, duration):
    """Exact interval integration, overlap included, zero collar, threshold 0.5.

    Global permutation minimizes total error, not per-frame oracle mapping.
    Reference annotations are utterance-level, not independently audited VAD.
    """
    frames, cursor = [], 0.0
    for event in value["events"]:
        if event["type"] != "speaker.activity":
            continue
        start, step = event["start_seconds"], event["frame_duration_seconds"]
        if abs(start - cursor) > 1e-5 or not math.isclose(step, .08):
            raise ValueError("Non-contiguous or unsupported diarization timestamps")
        for row in event["probabilities"]:
            if len(row) != 4 or any(not math.isfinite(p) or not 0 <= p <= 1 for p in row):
                raise ValueError("Invalid speaker probabilities")
            frames.append((cursor, min(duration, cursor + step), {i for i, p in enumerate(row) if p >= .5}))
            cursor += step
    if abs(value["audio_seconds"] - duration) > .001 or cursor < duration - .56:
        raise ValueError("Input duration or output coverage mismatch")
    segments = [(max(0, a), min(duration, b), speaker) for speaker, spans in enumerate(references)
                for a, b, *_ in spans if b > 0 and a < duration]
    boundaries = sorted({0., duration, *[x for a, b, _ in segments for x in (a, b)],
                         *[x for a, b, _ in frames if a < duration for x in (a, b)]})
    rows, frame_index = [], 0
    for a, b in zip(boundaries, boundaries[1:]):
        if b <= a:
            continue
        t = (a + b) / 2
        while frame_index < len(frames) and frames[frame_index][1] <= t:
            frame_index += 1
        predicted = frames[frame_index][2] if frame_index < len(frames) and frames[frame_index][0] <= t else set()
        actual = {speaker for start, end, speaker in segments if start <= t < end}
        rows.append((b-a, actual, predicted))
    best = None
    for mapping in itertools.permutations(range(4), len(references)):
        miss = false = confusion = denominator = 0.
        for seconds, actual, predicted in rows:
            correct = len({mapping[i] for i in actual} & predicted)
            miss += seconds * max(0, len(actual) - len(predicted))
            false += seconds * max(0, len(predicted) - len(actual))
            confusion += seconds * (min(len(actual), len(predicted)) - correct)
            denominator += seconds * len(actual)
        candidate = {"DER": (miss + false + confusion) / denominator,
                     "missed_speaker_seconds": miss, "false_alarm_speaker_seconds": false,
                     "confused_speaker_seconds": confusion, "reference_speaker_seconds": denominator,
                     "reference_to_model_speaker": list(mapping)}
        if best is None or candidate["DER"] < best["DER"]:
            best = candidate
    return {**best, "threshold": .5, "collar_seconds": 0, "overlap_included": True,
            "reference_granularity": "original human utterance TextGrid intervals",
            "predicted_frame_count": len(frames), "predicted_end_seconds": cursor,
            "audio_seconds": duration, "padding_seconds": max(0, cursor-duration),
            "service_integrity_pass": True, "clinical_or_paper_reproduction_claim": False}


def wav_integrity(data):
    with wave.open(io.BytesIO(data)) as audio:
        rate, channels, width, frames = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
        pcm = audio.readframes(frames)
    if rate != 22050 or channels != 1 or width != 2 or len(pcm) != frames*2 or frames == 0:
        raise ValueError("Invalid complete Magpie WAV")
    samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
    if not any(samples):
        raise ValueError("Silent TTS result")
    return {"service_integrity_pass": True, "sha256": sha(data), "size_bytes": len(data),
            "sample_rate_hz": rate, "frames": frames, "duration_seconds": frames/rate,
            "pcm_sha256": sha(pcm), "peak": max(abs(s) for s in samples)/32768,
            "rms": math.sqrt(sum(s*s for s in samples)/len(samples))/32768,
            "intelligibility_verified": False}, pcm


def prepare(args, save):
    manifest = json.loads((args.campaign / "datasets/speech-v1/cases.json").read_text())
    sources = manifest["cases"]
    english = [t for _, _, t in intervals(args.assets / "references/en/day1_consultation01_doctor.TextGrid")]
    english = [re.sub(r"<[^>]+>", "", t).strip() for t in english]
    german = [c for c in sources if c["expected"]["language"] == "de"]
    cases = []
    for index, voice in enumerate(("Sofia", "Jason", "Aria", "John", "Leo")):
        lang = "de" if index >= 3 else "en"
        text = german[index-3]["expected"]["reference_text"] if lang == "de" else " ".join(english[index:index+1+index*2])
        cases.append({"id": f"tts-native-{voice.lower()}-{lang}", "model": MAGPIE, "mode": "native",
                      "arguments": {"text": text, "voice": voice, "language": lang},
                      "text_source": "MultiMed pinned speech-v1 reference" if lang == "de" else "PriMock57 doctor TextGrid"})
    for index, lang in enumerate(("en", "de")):
        text = " ".join(english[:8]) if lang == "en" else " ".join(c["expected"]["reference_text"] for c in german[:8:2])
        cases.append({"id": f"tts-stream-{lang}", "model": MAGPIE, "mode": "tts-stream",
                      "arguments": {"text": text, "voice": "Sofia" if index == 0 else "Jason", "language": lang},
                      "text_source": "concatenated source-grounded utterances, not a natural original conversation"})
    for consultation in (1, 2):
        source = next(c for c in sources if c["case_id"].endswith(f"primock57-en-0{consultation}-r1"))
        field = source["preparation"]["artifact_fields"][0]
        audio = args.campaign / "datasets/speech-v1" / field["local_path"]
        if sha(audio.read_bytes()) != field["sha256"]:
            raise ValueError("Source audio hash mismatch")
        references = [args.assets / f"references/en/day1_consultation0{consultation}_{role}.TextGrid" for role in ("doctor", "patient")]
        cases.append({"id": f"diar-native-consultation-{consultation}", "model": SORTFORMER, "mode": "native",
                      "audio": str(audio), "audio_sha256": field["sha256"],
                      "duration": source["expected"]["audio_seconds"],
                      "references": [{"path": str(p), "sha256": sha(p.read_bytes())} for p in references],
                      "provenance": source["provenance"]})
        if consultation == 1:
            crop = args.output / "primock57-en-01-first60.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-t", "60", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(crop)], check=True)
            for mode in ("native", "diar-stream"):
                cases.append({**cases[-1 if mode == "native" else -2], "id": f"diar-{mode}-first60",
                              "mode": mode, "audio": str(crop), "audio_sha256": sha(crop.read_bytes()), "duration": 60.})
    save(args.output / "cases.json", {"cases": cases, "source_manifest_sha256": sha((args.campaign / "datasets/speech-v1/cases.json").read_bytes())})


def reconcile(client, key):
    cursor, found = None, []
    while True:
        response = client.get("/v1/operations", params={"limit": 100, **({"cursor": cursor} if cursor else {})})
        response.raise_for_status()
        value = response.json()
        rows = value.get("items", value.get("operations", []))
        found.extend(r for r in rows if r.get("idempotency_key") == key)
        cursor = value.get("next_cursor")
        if not cursor:
            break
    if len(found) > 1:
        raise ValueError("Idempotency key resolved to multiple operations")
    return found[0] if found else None


def terminal(client, operation_id, folder, save, timeout=1900):
    started = time.monotonic()
    while time.monotonic()-started < timeout:
        response = client.get(f"/v1/operations/{operation_id}")
        response.raise_for_status()
        value = response.json()
        save(folder / "operation.json", value)
        with (folder / "status.jsonl").open("a") as stream:
            stream.write(json.dumps({"observed_unix": time.time(), "status": value["status"]}) + "\n")
        if value["status"] in TERMINAL:
            return value
        time.sleep(3)
    raise TimeoutError("Operation still pending; stop before new admission")


async def diar_stream(client, token, case, key, folder, save, origin):
    from websockets.asyncio.client import connect
    pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", case["audio"], "-f", "s16le", "-ar", "16000", "-ac", "1", "-"])
    events, operation_id, task = [], None, None
    started = time.monotonic()
    async with connect(origin.replace("https://", "wss://").replace("http://", "ws://") + "/v1/voice/stream",
                       additional_headers={"authorization": "Bearer " + token, "idempotency-key": key}, proxy=None,
                       open_timeout=30, max_size=2**20) as socket:
        await socket.send(json.dumps({"type": "session.start", "model": case["model"], "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}}))

        async def upload():
            t0 = time.monotonic()
            for offset in range(0, len(pcm), 16000):
                await socket.send(pcm[offset:offset+16000])
                await asyncio.sleep(max(0, t0+(offset+16000)/32000-time.monotonic()))
            await socket.send('{"type":"session.finish"}')

        try:
            async for raw in socket:
                event = json.loads(raw)
                events.append({"received_seconds": time.monotonic()-started, **event})
                if event.get("operation_id"):
                    operation_id = event["operation_id"]
                    save(folder / "admission.json", {"operation_id": operation_id, "idempotency_key": key})
                save(folder / "stream-events.json", events)
                if event["type"] == "session.ready":
                    task = asyncio.create_task(upload())
                if event["type"] in {"session.done", "session.cancelled", "error"}:
                    break
            if task:
                await task
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    if not operation_id:
        raise ValueError("No durable voice operation ID; reconcile before retry")
    return operation_id


def run_case(mcp, person, case, args, save, upload, artifact):
    folder = args.output / case["id"]
    folder.mkdir(mode=0o700, exist_ok=True)
    key = "voice-20260918-r1-" + sha(json.dumps(case, sort_keys=True).encode())[:32]
    receipt_path = folder / "receipt.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {"case": case["id"], "model": case["model"], "mode": case["mode"], "idempotency_key": key, "started_unix": time.time(), "state": "prepared"}
    if receipt["state"] in {"verified_integrity", "failed", "cancelled", "expired", "preempted"}:
        return receipt
    save(receipt_path, receipt)
    prior = reconcile(mcp.client, key)
    if prior:
        operation_id = prior["id"]
    elif case["mode"] == "native":
        arguments = dict(case.get("arguments", {}))
        if "audio" in case:
            data = Path(case["audio"]).read_bytes()
            if sha(data) != case["audio_sha256"]:
                raise ValueError("Input changed")
            ref = upload(mcp.client, case["model"], data, "audio/wav" if case["audio"].endswith("wav") else "audio/flac", "none", key+"-input")
            arguments["audio"] = ref
        arguments.update(idempotency_key=key, wait_seconds=0)
        save(folder / "request.json", arguments)
        tool = "infer_" + case["model"].replace("-", "_") + "_native"
        accepted = mcp.call(tool, arguments)
        save(folder / "submission.json", accepted)
        op = accepted["operation"] if isinstance(accepted.get("operation"), dict) else accepted
        operation_id = op["id"]
    elif case["mode"] == "tts-stream":
        events, pcm, operation_id = [], bytearray(), None
        started = time.monotonic()
        with mcp.client.stream("POST", "/v1/voice/synthesize", json={"model": MAGPIE, **case["arguments"]}, headers={"idempotency-key": key}, timeout=300) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event.get("operation_id"):
                    operation_id = event["operation_id"]
                    save(folder / "admission.json", {"operation_id": operation_id, "idempotency_key": key})
                if event["type"] == "audio.chunk":
                    if event["sequence"] != sum(e["type"] == "audio.chunk" for e in events):
                        raise ValueError("PCM stream sequence gap")
                    chunk = base64.b64decode(event.pop("audio_base64"), validate=True)
                    pcm.extend(chunk)
                    event.update(bytes=len(chunk), sha256=sha(chunk))
                events.append({"received_seconds": time.monotonic()-started, **event})
                save(folder / "stream-events.json", events)
        (folder / "stream.pcm").write_bytes(pcm)
        if not operation_id:
            raise ValueError("No durable operation; reconcile before retry")
    else:
        from manage_campaign import ORIGIN
        operation_id = asyncio.run(diar_stream(mcp.client, person["api_key"], case, key, folder, save, ORIGIN))
    receipt.update(operation_id=operation_id, state="admitted")
    save(receipt_path, receipt)
    operation = terminal(mcp.client, operation_id, folder, save)
    receipt.update(state=operation["status"], elapsed_seconds=time.time()-receipt["started_unix"],
                   runtime=operation.get("runtime"), error_code=operation.get("error_code"))
    save(receipt_path, receipt)
    if operation["status"] != "succeeded":
        return receipt
    response = mcp.client.get(f"/v1/operations/{operation_id}/result")
    response.raise_for_status()
    envelope = response.json()
    save(folder / "result-envelope.json", envelope)
    value = envelope.get("result", envelope)
    if value.get("schema") == "fs2-serve.nebius.ai/operation-artifact-result/v1":
        raw = artifact(mcp.client, value["artifact"], folder)
        if "json" in value.get("content_type", ""):
            value = json.loads(raw)
    elif case["model"] == MAGPIE:
        # Inline result envelopes are deliberately not assumed to be base64.
        raise ValueError("Inspect unexpected Magpie result before decoding")
    if case["model"] == MAGPIE:
        evaluation, pcm = wav_integrity(raw)
        (folder / "output.wav").write_bytes(raw)
        if case["mode"] == "tts-stream" and (folder / "stream.pcm").exists():
            if pcm != (folder / "stream.pcm").read_bytes():
                raise ValueError("Stream and retained WAV differ")
            evaluation["stream_matches_retained_wav"] = True
    else:
        save(folder / "result.json", value)
        for ref in case["references"]:
            if sha(Path(ref["path"]).read_bytes()) != ref["sha256"]:
                raise ValueError("Reference changed")
        evaluation = diarization_score(value, [intervals(Path(r["path"])) for r in case["references"]], case["duration"])
    save(folder / "evaluation.json", evaluation)
    receipt.update(state="verified_integrity", evaluation=evaluation)
    save(receipt_path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--client-dir", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    sys.path.insert(0, str(args.client_dir))
    from manage_campaign import save
    if args.prepare:
        prepare(args, save)
        return
    from run_campaign import MCP, artifact
    from batch_transport import upload
    import fcntl
    person = next(p for p in json.loads((args.campaign / "scientists-private.json").read_text())["scientists"] if p["id"] == "scientist-09")
    with (args.output / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mcp = MCP(person["api_key"])
        try:
            save(args.output / "initialize.json", mcp.initialize())
            save(args.output / "tools.json", mcp.rpc("tools/list"))
            results = []
            for case in json.loads((args.output / "cases.json").read_text())["cases"]:
                if args.only and case["id"] not in args.only.split(","):
                    continue
                try:
                    result = run_case(mcp, person, case, args, save, upload, artifact)
                except Exception as error:
                    import traceback
                    save(args.output / case["id"] / "harness-error.json", {"error_type": type(error).__name__, "traceback": traceback.format_exc()})
                    print(json.dumps({"case": case["id"], "state": "stopped_needs_reconciliation", "error_type": type(error).__name__}), flush=True)
                    return
                results.append(result)
                save(args.output / "summary.json", results)
                print(json.dumps({k: result.get(k) for k in ("case", "state", "operation_id", "elapsed_seconds")}), flush=True)
        finally:
            mcp.close()


if __name__ == "__main__":
    main()
