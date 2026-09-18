#!/usr/bin/env python3
"""Offline measurements and optional frozen ASR readback manifest, never admission."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import struct

from qualify import sha, wav_integrity


def measure(folder):
    operation = json.loads((folder / "operation.json").read_text())
    result = {"operation_id": operation["id"], "status": operation["status"],
              "runtime": operation.get("runtime"), "cold_start_seconds": operation.get("cold_start_seconds"),
              "modality_usage": operation.get("modality_usage"), "attempt": operation.get("attempt")}
    def difference(a, b):
        if operation.get(a) and operation.get(b):
            return (datetime.fromisoformat(operation[b])-datetime.fromisoformat(operation[a])).total_seconds()
        return None
    result["accepted_to_started_seconds"] = difference("accepted_at", "started_at")
    result["started_to_completed_seconds"] = difference("started_at", "completed_at")
    result["accepted_to_completed_seconds"] = difference("accepted_at", "completed_at")
    if (folder / "output.wav").exists():
        integrity, pcm = wav_integrity((folder / "output.wav").read_bytes())
        samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
        result.update(integrity)
        result["near_zero_sample_fraction_abs_lt_33"] = sum(abs(v) < 33 for v in samples)/len(samples)
        result["near_clipping_sample_fraction_abs_ge_32760"] = sum(abs(v) >= 32760 for v in samples)/len(samples)
        elapsed = result["started_to_completed_seconds"]
        result["observed_operation_RTF"] = elapsed/integrity["duration_seconds"] if elapsed else None
        result["RTF_scope"] = "operation start-to-completion includes transport/publication; not isolated model compute"
    if (folder / "stream-events.json").exists():
        events = json.loads((folder / "stream-events.json").read_text())
        chunks = [e for e in events if e["type"] == "audio.chunk"]
        activity = [e for e in events if e["type"] == "speaker.activity"]
        result["stream_event_count"] = len(events)
        result["stream_terminal_type"] = events[-1]["type"] if events else None
        result["first_audio_seconds"] = chunks[0]["received_seconds"] if chunks else None
        result["last_audio_seconds"] = chunks[-1]["received_seconds"] if chunks else None
        result["first_speaker_activity_seconds"] = activity[0]["received_seconds"] if activity else None
        result["stream_total_seconds"] = events[-1]["received_seconds"] if events else None
        result["stream_error_codes"] = [e.get("code") for e in events if e["type"] == "error"]
        if chunks:
            result["max_inter_chunk_seconds"] = max((b["received_seconds"]-a["received_seconds"] for a,b in zip(chunks,chunks[1:])), default=0)
    if (folder / "evaluation.json").exists():
        result["evaluation"] = json.loads((folder / "evaluation.json").read_text())
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scorer", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads((args.output / "cases.json").read_text())["cases"]
    measured, readbacks = {}, []
    for case in cases:
        folder = args.output / case["id"]
        if not (folder / "operation.json").exists():
            continue
        measured[case["id"]] = measure(folder)
        audio = folder / "output.wav"
        if audio.exists():
            data = audio.read_bytes()
            readbacks.append({"case_id": "asr-proxy-"+case["id"], "model_id": "nemotron-speech-multilingual-0-6b",
                              "tool": "infer_nemotron_speech_multilingual_0_6b_native", "mode": "native",
                              "arguments": {"options": {"model": "nemotron-speech-multilingual-0.6b",
                                            "language": case["arguments"]["language"], "output_granularity": "word"}},
                              "preparation": {"artifact_fields": [{"field": "audio", "transport": "artifact",
                              "local_path": str(audio), "media_type": "audio/wav", "compression": "none",
                              "sha256": sha(data), "size_bytes": len(data)}]},
                              "expected": {"evaluator": "speech_transcription", "reference_text": case["arguments"]["text"],
                                           "language": case["arguments"]["language"],
                                           "audio_seconds": measured[case["id"]]["duration_seconds"],
                                           "scorer_path": str(args.scorer), "scorer_sha256": sha(args.scorer.read_bytes())},
                              "provenance": {"source_tts_operation_id": measured[case["id"]]["operation_id"],
                                             "scope": "ASR round-trip proxy; dual-model errors, not TTS ground truth or clinical validation"}})
    (args.output / "measurements.json").write_text(json.dumps(measured, indent=2)+"\n")
    target = args.output / "asr-proxy-cases.json"
    value = {"schema_version": 1, "study_id": "voice-tts-asr-roundtrip-proxy", "cases": readbacks}
    if target.exists() and json.loads(target.read_text()) != value:
        raise ValueError("Refusing to change a frozen readback manifest")
    target.write_text(json.dumps(value, indent=2)+"\n")
    print(json.dumps({"measured_cases": len(measured), "readback_cases": len(readbacks)}))


if __name__ == "__main__":
    main()
