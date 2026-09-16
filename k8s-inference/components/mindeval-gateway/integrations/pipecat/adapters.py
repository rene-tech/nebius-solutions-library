"""Pipecat 1.10.0 frame adapters for authenticated Scientific AI endpoints.

These are finite-utterance adapters: no microphone/VAD/WebRTC transport is
pretended. The ordinary platform PAT is sent only to one configured origin.
"""

import asyncio
import base64
import io
import json
import re
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pipecat.frames.frames import (
    DataFrame,
    ErrorFrame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

MAGPIE = "magpie-tts-multilingual-357m"
MAX_AUDIO = 8 * 1024 * 1024 - 44


def word_error_rate(reference, hypothesis):
    """Case/punctuation-normalized Levenshtein WER; not semantic accuracy."""
    expected = re.findall(r"\w+", reference.casefold())
    actual = re.findall(r"\w+", hypothesis.casefold())
    if not expected:
        return None
    previous = list(range(len(actual) + 1))
    for i, word in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(min(previous[j] + 1, current[-1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(expected)


def wav_bytes(audio, sample_rate):
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(audio)
    return target.getvalue()


class PlatformClient:
    def __init__(self, client: httpx.AsyncClient, token: str, timeout=600):
        self.client, self.token, self.timeout = client, token, timeout

    def headers(self, key=None):
        result = {"Authorization": f"Bearer {self.token}"}
        if key:
            result["Idempotency-Key"] = key
        return result

    async def json(self, method, path, body=None):
        response = await self.client.request(method, path, headers=self.headers(), json=body)
        if response.status_code != 200:
            raise RuntimeError(f"platform {path.split('?')[0]} returned HTTP {response.status_code}")
        return response.json()

    async def transcribe(self, pcm, sample_rate, model="nemotron-speech-en-0-6b"):
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_AUDIO:
            raise ValueError("transcription requires a nonempty bounded PCM16 utterance")
        response = await self.client.post(
            "/v1/audio/transcriptions",
            headers=self.headers(f"pipecat-stt-{uuid4()}"),
            data={"model": model},
            files={"file": ("utterance.wav", wav_bytes(pcm, sample_rate), "audio/wav")},
        )
        if response.status_code == 202:
            location = response.headers.get("location", "")
            if not re.fullmatch(r"/v1/operations/[0-9a-fA-F-]{36}", location):
                raise RuntimeError("STT returned an invalid operation location")
            async with asyncio.timeout(self.timeout):
                while True:
                    status = await self.json("GET", location)
                    if status.get("status") == "succeeded":
                        response = await self.client.get(location + "/result", headers=self.headers())
                        break
                    if status.get("status") in {"failed", "cancelled", "expired"}:
                        raise RuntimeError(f"STT operation {status['status']}")
                    await asyncio.sleep(1)
        if response.status_code != 200:
            raise RuntimeError(f"STT returned HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result.get("text"), str) or not result["text"].strip():
            raise RuntimeError("STT returned no transcript")
        return result


@dataclass
class ModelTurnFrame(DataFrame):
    role: str


class CheckedProcessor(FrameProcessor):
    def __init__(self, failures, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction is FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return
        try:
            await self.handle(frame, direction)
        except Exception as exc:
            # No HTTP headers, credential bytes, raw exception response or
            # stack trace is copied to the attendee artifact.
            error = {"processor": self.name, "type": type(exc).__name__, "message": str(exc)}
            self.failures.append(error)
            await self.push_frame(ErrorFrame(error=json.dumps(error)), direction)


class MindEvalProcessor(CheckedProcessor):
    def __init__(self, api, run_id, profile, patient_model, clinician_model, failures):
        super().__init__(failures)
        self.api, self.run_id, self.profile = api, run_id, profile
        self.models = {"patient": patient_model, "clinician": clinician_model}
        self.transcript = [{"role": "patient", "content": "Hello", "seed": True}]

    async def handle(self, frame, direction):
        if not isinstance(frame, ModelTurnFrame):
            await self.push_frame(frame, direction)
            return
        if self.failures:
            return
        role = frame.role
        if role not in self.models or role == self.transcript[-1]["role"]:
            raise ValueError("canonical model roles must alternate after initial patient Hello")
        messages = [{"role": "system", "content": self.profile[f"{role}_system_prompt"]}]
        messages += [
            {"role": "assistant" if turn["role"] == role else "user", "content": turn["content"]}
            for turn in self.transcript
        ]
        await self.push_frame(LLMFullResponseStartFrame())
        result = await self.api.json(
            "POST",
            "/v1/mindeval/completions",
            {
                "run_id": self.run_id,
                "profile_id": self.profile["id"],
                "role": role,
                "model": self.models[role],
                "messages": messages,
                "max_completion_tokens": 4096,
            },
        )
        if not result.get("content", "").strip() or result.get("finish_reason") != "stop":
            raise RuntimeError("gateway did not return a complete visible answer")
        context = {"role": role, "index": len(self.transcript), "completion": result}
        self.transcript.append({"role": role, "content": result["content"], "completion": result})
        text = LLMTextFrame(result["content"])
        text.metadata.update(context)
        await self.push_frame(text)
        await self.push_frame(LLMFullResponseEndFrame())


class MagpieProcessor(CheckedProcessor):
    def __init__(self, api, failures, voices=None):
        super().__init__(failures)
        self.api = api
        self.voices = voices or {"patient": "Sofia", "clinician": "Jason"}

    async def handle(self, frame, direction):
        await self.push_frame(frame, direction)
        if not isinstance(frame, LLMTextFrame) or self.failures:
            return
        if len(frame.text) > 4096:
            raise ValueError("Magpie accepts at most 4096 characters; this reference does not segment or truncate text")
        voice = self.voices[frame.metadata["role"]]
        context = {**frame.metadata, "voice": voice, "text": frame.text}
        context_id = str(uuid4())
        started, first_audio, samples, sequence = time.monotonic(), None, 0, 0
        audio_started, done = False, None
        async with self.api.client.stream(
            "POST",
            "/v1/voice/synthesize",
            headers=self.api.headers(f"pipecat-tts-{context_id}"),
            json={
                "model": MAGPIE,
                "text": frame.text,
                "voice": voice,
                "language": "en",
                "apply_text_normalization": False,
            },
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Magpie returned HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line:
                    continue
                if len(line) > 65536 or done is not None:
                    raise RuntimeError("invalid or post-completion Magpie event")
                event = json.loads(line)
                kind = event.get("type")
                if kind == "operation.queued":
                    continue
                if kind == "audio.start":
                    if (
                        audio_started
                        or event.get("encoding") != "pcm_s16le"
                        or event.get("sample_rate_hz") != 22050
                        or event.get("channels") != 1
                        or event.get("model") != MAGPIE
                        or event.get("voice") != voice
                    ):
                        raise RuntimeError("Magpie audio format changed")
                    audio_started = True
                    context["tts_start"] = event
                    start = TTSStartedFrame(context_id=context_id)
                    start.metadata.update(context)
                    await self.push_frame(start)
                elif kind == "audio.chunk":
                    pcm = base64.b64decode(event["audio_base64"], validate=True)
                    if (
                        not audio_started
                        or event.get("sequence") != sequence
                        or event.get("sample_rate_hz") != 22050
                        or not pcm
                        or len(pcm) % 2
                        or (samples * 2 + len(pcm)) > MAX_AUDIO
                    ):
                        raise RuntimeError("Magpie audio sequence, sample rate or length is invalid")
                    if first_audio is None:
                        first_audio = time.monotonic() - started
                    samples += len(pcm) // 2
                    sequence += 1
                    audio = TTSAudioRawFrame(pcm, 22050, 1, context_id=context_id)
                    audio.metadata.update(context)
                    await self.push_frame(audio)
                elif kind == "audio.done":
                    if not samples or event.get("samples") != samples or event.get("chunks") != sequence:
                        raise RuntimeError("Magpie completion does not match received audio")
                    done = event
                elif kind == "error":
                    raise RuntimeError(f"Magpie failed: {str(event.get('code', 'unknown'))[:128]}")
                else:
                    raise RuntimeError(f"Magpie unexpected event: {kind}")
        if done is None:
            raise RuntimeError("Magpie stream ended without audio.done")
        stop = TTSStoppedFrame(context_id=context_id)
        stop.metadata.update(
            {**context, "tts": done, "first_audio_seconds": first_audio, "tts_seconds": time.monotonic() - started}
        )
        await self.push_frame(
            MetricsFrame(
                [
                    TTFBMetricsData(processor=self.name, value=first_audio),
                    ProcessingMetricsData(processor=self.name, value=stop.metadata["tts_seconds"]),
                ]
            )
        )
        await self.push_frame(stop)


class NemotronSTTProcessor(CheckedProcessor):
    """InputAudioRawFrame must contain one COMPLETE PCM16 mono utterance.

    audit_tts additionally transcribes generated speech without altering the
    canonical MindEval transcript. It is a round-trip measurement, not VAD.
    """

    def __init__(self, api, failures, audit_tts=True, model="nemotron-speech-en-0-6b"):
        super().__init__(failures)
        self.api, self.audit_tts, self.model = api, audit_tts, model
        self.pending = {}
        self.observations = []

    async def recognize(self, pcm, rate, context):
        started = time.monotonic()
        result = await self.api.transcribe(pcm, rate, self.model)
        record = {**context, "asr": result, "stt_seconds": time.monotonic() - started}
        record["word_error_rate"] = word_error_rate(context.get("text", ""), result["text"])
        self.observations.append(record)
        text = TranscriptionFrame(
            result["text"],
            context.get("role", "attendee"),
            datetime.now(UTC).isoformat(),
            result=result,
            finalized=True,
        )
        text.metadata.update(record)
        await self.push_frame(text)
        await self.push_frame(RTVIServerMessageFrame(data={"type": "fs2.speech.roundtrip", **record}))

    async def handle(self, frame, direction):
        if isinstance(frame, InputAudioRawFrame):
            if frame.num_channels != 1:
                raise ValueError("STT requires mono PCM16 input")
            await self.recognize(frame.audio, frame.sample_rate, frame.metadata)
            return
        await self.push_frame(frame, direction)
        if not self.audit_tts or self.failures:
            return
        if isinstance(frame, TTSAudioRawFrame):
            self.pending.setdefault(frame.context_id, bytearray()).extend(frame.audio)
        elif isinstance(frame, TTSStoppedFrame):
            pcm = bytes(self.pending.pop(frame.context_id, b""))
            await self.recognize(pcm, 22050, frame.metadata)
