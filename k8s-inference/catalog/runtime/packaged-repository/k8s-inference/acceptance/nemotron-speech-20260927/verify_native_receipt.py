"""Verify retained direct-worker native protocol evidence, not customer readiness.

The probe produces two complete-file results and playback-paced live event logs.
This offline verifier checks their actual bytes, checkpoint identity and parity.
No request, signed artifact URL or authorization material is printed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import wave


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def transcript_text(events):
    text = ""
    for item in events:
        event = item["event"]
        if event["type"] != "transcript.final":
            continue
        piece = event["text"]
        separator = event.get("separator_before", "")
        assert separator in ("", " ")
        if separator == " " and text and piece and not text[-1].isspace() and not piece[0].isspace():
            text += " "
        text += piece
    return text.strip()


def verify(directory: Path, checkpoint: str):
    receipt = json.loads((directory / "receipt.json").read_bytes())
    assert receipt["status"] == "PASS_TWO_NATIVE_FILES_AND_PACED_LIVE_PARITY"
    assert receipt["checkpoint_sha256"] == checkpoint
    assert receipt["gateway_qualified"] is False and receipt["clinical_qualified"] is False
    assert receipt["capacity_measured"] is False
    rows = receipt["measurements"]
    assert len(rows) == 2 and len({r["case"] for r in rows}) == 2
    responses, requests = set(), set()
    for index, row in enumerate(rows):
        assert row["case"] in {"speech-fixture-observatory", "speech-fixture-workshop"}
        audio = directory / (row["case"] + ".wav")
        assert digest(audio) == row["input_sha256"] and audio.stat().st_size == row["input_bytes"]
        with wave.open(str(audio)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            seconds = wav.getnframes() / 16000
        request = directory / f"request-{index}.json"
        response = directory / f"file-response-{index}.json"
        events = directory / f"live-events-{index}.json"
        assert digest(request) == row["request_payload_sha256"]
        assert digest(response) == row["response_sha256"]
        assert digest(events) == row["live_events_sha256"]
        result = json.loads(response.read_bytes())
        payload = json.loads(request.read_bytes())
        assert payload["audio"]["sha256"] == row["input_sha256"]
        assert payload["audio"]["size_bytes"] == row["input_bytes"]
        assert result["runtime_identity"]["checkpoint_sha256"] == checkpoint
        assert result["model_revision"] == "sha256:" + checkpoint
        assert abs(seconds - result["audio_seconds"]) < 1 / 16000
        transcript_events = json.loads(events.read_bytes())
        first = transcript_events[0]["event"]
        assert first["type"] == "session.ready"
        assert first["runtime_identity"]["checkpoint_sha256"] == checkpoint
        assert not any(e["event"]["type"] == "session.error" for e in transcript_events)
        final = transcript_events[-1]["event"]
        assert final["type"] == "session.completed" and final["audio_seconds"] == result["audio_seconds"]
        live_text = transcript_text(transcript_events)
        assert live_text == result["text"] and live_text
        assert row["live_wall_seconds"] >= seconds
        responses.add(live_text)
        requests.add(digest(request))
    assert len(responses) == len(requests) == 2
    return {"status": "PASS_RETAINED_NATIVE_PROTOCOL_EVIDENCE", "checkpoint_sha256": checkpoint,
            "receipt_sha256": digest(directory / "receipt.json"), "customer_ready": False,
            "capacity_measured": False, "clinical_qualified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, args.checkpoint)))
