"""Finite two-voice Pipecat/RTVI reference; connects to existing services only."""

import argparse
import asyncio
import hashlib
import json
import math
import ssl
import time
from pathlib import Path
from uuid import uuid4

import httpx
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    InputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi.models import PROTOCOL_VERSION
from pipecat.workers.runner import WorkerRunner

from adapters import MagpieProcessor, MindEvalProcessor, ModelTurnFrame, NemotronSTTProcessor, PlatformClient, wav_bytes

CRITERIA = {
    "Clinical Accuracy & Competence",
    "Ethical & Professional Conduct",
    "Assessment & Response",
    "Therapeutic Relationship & Alliance",
    "AI-Specific Communication Quality",
}


class Recorder(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames, self.rtvi, self.audio, self.pending = [], [], [], {}

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(type(frame).__name__)
        if isinstance(frame, OutputTransportMessageUrgentFrame):
            self.rtvi.append(frame.message)
        if isinstance(frame, TTSAudioRawFrame):
            self.pending.setdefault(frame.context_id, bytearray()).extend(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            pcm = bytes(self.pending.pop(frame.context_id, b""))
            self.audio.append({"pcm": pcm, "sample_rate": 22050, **frame.metadata})
        await self.push_frame(frame, direction)


async def run_pipeline(api, *, run_id, profile_id, patient_model, clinician_model, rounds=1):
    registration = await api.json(
        "POST",
        f"/v1/mindeval/runs/{run_id}/register",
        {
            "profile_ids": [profile_id],
            "patient_model": patient_model,
            "clinician_models": [clinician_model],
        },
    )
    profile = await api.json("GET", f"/v1/mindeval/profiles/{profile_id}")
    failures = []
    llm = MindEvalProcessor(api, run_id, profile, patient_model, clinician_model, failures)
    tts = MagpieProcessor(api, failures)
    stt = NemotronSTTProcessor(api, failures)
    recorder = Recorder()
    worker = PipelineWorker(
        Pipeline([llm, tts, stt, recorder]),
        params=PipelineParams(audio_out_sample_rate=22050, enable_metrics=True),
        enable_rtvi=True,
        idle_timeout_secs=1200,
    )
    ready = asyncio.Event()

    @worker.rtvi.event_handler("on_client_ready")
    async def on_ready(_processor):
        ready.set()

    async def feed():
        # A genuine RTVI protocol handshake over Pipecat transport frames.
        # This reference captures the wire messages; it does not claim a
        # deployed browser/WebRTC transport or microphone connection.
        await worker.queue_frames(
            [
                InputTransportMessageFrame(
                    message={
                        "label": "rtvi-ai",
                        "type": "client-ready",
                        "id": "fs2-cli-ready",
                        "data": {"version": PROTOCOL_VERSION, "about": {"library": "fs2-pipecat-reference"}},
                    }
                )
            ]
        )
        await asyncio.wait_for(ready.wait(), 15)
        await worker.queue_frames(
            [
                *(ModelTurnFrame(role) for _ in range(rounds) for role in ("clinician", "patient")),
                EndFrame(),
            ]
        )

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    feeding = asyncio.create_task(feed())
    try:
        async with asyncio.timeout(1800):
            await asyncio.gather(runner.run(), feeding)
    except BaseException:
        await runner.cancel()
        raise
    finally:
        if not feeding.done():
            feeding.cancel()
            await asyncio.gather(feeding, return_exceptions=True)
    report = {
        "schema": "fs2-pipecat-reference/v1",
        "pipecat_version": "1.10.0",
        "pipecat_revision": "f67c18afddbfb0609991cd6830355713baaad01b",
        "rtvi_protocol": PROTOCOL_VERSION,
        "transport": "in_process_reference_not_browser_webrtc",
        "run_id": run_id,
        "registration": registration,
        "profile": profile,
        "transcript": llm.transcript,
        "speech_observations": stt.observations,
        "rtvi_messages": recorder.rtvi,
        "frame_types": sorted(set(recorder.frames)),
        "failures": failures,
        "canonical_judgment": None,
    }
    if not failures:
        if len(llm.transcript) != 1 + rounds * 2 or len(stt.observations) != rounds * 2:
            failures.append({"message": "pipeline did not complete every generated and transcribed turn"})
        elif not any(message["type"] == "bot-ready" for message in recorder.rtvi):
            failures.append({"message": "RTVI handshake did not complete"})
        else:
            report["canonical_judgment"] = await api.json(
                "POST",
                "/v1/mindeval/judgments",
                {
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "clinician_model": clinician_model,
                    "interaction": [
                        {"role": "user" if item["role"] == "patient" else "assistant", "content": item["content"]}
                        for item in llm.transcript
                    ],
                },
            )
            scores = report["canonical_judgment"].get("judgment", {})
            if set(scores) != CRITERIA or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 1 <= value <= 6
                for value in scores.values()
            ):
                failures.append({"message": "gateway judgment did not contain five valid named criteria"})
    report["passed"] = not failures
    return report, recorder.audio


def token_from_file(path, team):
    raw = Path(path).read_text().strip()
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        item = (
            data["teams"][team]
            if isinstance(data, dict) and "teams" in data
            else data[team]
            if isinstance(data, list)
            else data
        )
        return item if isinstance(item, str) else item.get("token") or item["plaintext_token"]
    return raw


async def main_async(args):
    token = token_from_file(args.token_file, args.team)
    if not token:
        raise ValueError("empty platform PAT")
    verify = False if args.insecure else ssl.create_default_context(cafile=args.ca_file)
    started = time.monotonic()
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), verify=verify, timeout=600, trust_env=False
    ) as client:
        try:
            report, audio = await run_pipeline(
                PlatformClient(client, token),
                run_id=args.run_id,
                profile_id=args.profile,
                patient_model=args.patient,
                clinician_model=args.clinician,
                rounds=args.rounds,
            )
        except Exception as exc:
            report, audio = (
                {
                    "schema": "fs2-pipecat-reference/v1",
                    "passed": False,
                    "run_id": args.run_id,
                    "failures": [{"type": type(exc).__name__, "message": str(exc).replace(token, "[REDACTED]")}],
                },
                [],
            )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report["elapsed_seconds"] = time.monotonic() - started
    report["audio_files"] = []
    for item in audio:
        name = f"{item['index']:03}-{item['role']}-{item['voice']}.wav"
        content = wav_bytes(item["pcm"], item["sample_rate"])
        (output / name).write_bytes(content)
        report["audio_files"].append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "sample_rate": item["sample_rate"],
            }
        )
    rendered = json.dumps(report, indent=2)
    if token in rendered:
        raise RuntimeError("credential appeared in report; refusing to save")
    (output / "report.json").write_text(rendered + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "output": str(output),
                "audio_files": len(audio),
                "run_id": args.run_id,
                "elapsed_seconds": report["elapsed_seconds"],
            }
        )
    )
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--team", type=int, default=0)
    parser.add_argument("--run-id", default=f"pipecat-{uuid4()}")
    parser.add_argument("--profile", default="profile-000")
    parser.add_argument("--patient", default="Qwen/Qwen3-30B-A3B-Instruct-2507")
    parser.add_argument("--clinician", default="Qwen/Qwen3-235B-A22B-Instruct-2507")
    parser.add_argument("--rounds", type=int, choices=range(1, 21), default=1)
    parser.add_argument("--output", default="output")
    parser.add_argument("--ca-file")
    parser.add_argument("--insecure", action="store_true", help="Explicit self-signed rehearsal TLS exception")
    args = parser.parse_args()
    logger.remove()
    logger.add(lambda message: print(message, end=""), level="WARNING")
    raise SystemExit(0 if asyncio.run(main_async(args)) else 1)


if __name__ == "__main__":
    main()
