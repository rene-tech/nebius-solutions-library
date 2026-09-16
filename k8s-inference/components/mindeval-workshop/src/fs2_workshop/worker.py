"""One durable turn at a time, with explicit interruption and inference evidence."""

import asyncio
import base64
import contextlib
import io
import json
import logging
import re
import time
import wave
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx

from .models import judge_interaction, messages_for

DATABASE_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)
LOG = logging.getLogger(__name__)


class RemoteFailure(Exception):
    def __init__(self, code, message, status=None):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


async def json_call(client, method, url, token, *, body=None, host=None):
    headers = {"Authorization": f"Bearer {token}"}
    if host:
        headers["Host"] = host
    response = await client.request(method, url, headers=headers, json=body)
    if not response.is_success:
        # Provider errors are recorded by gateway; don't accidentally retain
        # stack traces, headers or credentials from an intermediate proxy.
        raise RemoteFailure("gateway_http_error", f"Gateway returned HTTP {response.status_code}", response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise RemoteFailure("invalid_response", "Gateway returned invalid JSON") from exc


def wav_bytes(pcm, rate):
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return target.getvalue()


def speech_segments(text, limit=4096):
    """Preserve every character, preferring sentence and then word boundaries."""
    segments = []
    while len(text) > limit:
        prefix = text[:limit]
        boundaries = list(re.finditer(r"[.!?][\"')\]]?\s+", prefix))
        cut = boundaries[-1].end() if boundaries else prefix.rfind(" ") + 1
        if cut <= 0 or not text[:cut].strip():
            cut = limit  # One overlong word still cannot exceed the provider bound.
        segments.append(text[:cut])
        text = text[cut:]
    if text:
        segments.append(text)
    return segments


async def synthesis_events(client, url, headers, text, role, config):
    # Keep each PCM/ASR upload practical, below the provider's 4096-char bound.
    for index, segment in enumerate(speech_segments(text, 1024)):
        complete, has_audio = None, False
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json={
                "model": "magpie-tts-multilingual-357m",
                "text": segment,
                "language": config["language"],
                "voice": config[f"{role}_voice"],
                "apply_text_normalization": False,
            },
        ) as response:
            if not response.is_success:
                raise RemoteFailure("tts_http_error", f"Speech generation returned HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event["type"] == "audio.chunk":
                    has_audio = True
                elif event["type"] == "audio.done":
                    complete = {**event, "segment_index": index, "input_characters": len(segment)}
                    continue
                yield event
        if not complete or not has_audio:
            raise RemoteFailure("tts_incomplete", "Speech generation did not deliver complete audio for every segment")
        yield complete  # Close the TTS response before uploading its bounded ASR segment.


class Worker:
    def __init__(self, store, settings, client):
        self.store, self.settings, self.client = store, settings, client
        self.owner = str(uuid4())
        self.stopping = False

    async def heartbeat(self, run_id):
        delay = self.settings.lease_seconds / 3
        while True:
            await asyncio.sleep(delay)
            try:
                await self.store.heartbeat(run_id, self.owner, self.settings.lease_seconds)
                delay = self.settings.lease_seconds / 3
            except DATABASE_ERRORS:
                # Retry delay is bounded; expired leases cannot commit a result.
                LOG.warning("Workshop heartbeat database unavailable; lease fencing remains active")
                delay = min(5, max(1, delay / 2))

    async def finish(self, *args, **kwargs):
        try:
            return await self.store.finish_step(*args, **kwargs)
        except DATABASE_ERRORS:
            # Do not replay an uncertain provider call. A surviving/next worker
            # marks this expired lease interrupted and requires explicit Resume.
            LOG.warning("Workshop finish database unavailable; leaving lease for explicit interrupted recovery")
            return False

    async def loop(self):
        failures = 0
        while not self.stopping:
            try:
                row = await self.store.claim(self.owner, self.settings.lease_seconds, self.settings.max_team_workers)
                failures = 0
            except DATABASE_ERRORS:
                failures += 1
                LOG.warning("Workshop claim database unavailable; retrying without replaying active work")
                await asyncio.sleep(min(5, failures))
                continue
            if row is None:
                await asyncio.sleep(0.5)
                continue
            heartbeat = asyncio.create_task(self.heartbeat(row["id"]))
            started = time.monotonic()
            try:
                await self.step(row)
            except asyncio.CancelledError:
                await self.finish(
                    row,
                    row["state"],
                    "interrupted",
                    "run.interrupted",
                    {"reason": "worker shutdown; resume explicitly"},
                )
                raise
            except DATABASE_ERRORS:
                await self.finish(
                    row,
                    row["state"],
                    "interrupted",
                    "run.interrupted",
                    {"reason": "database interruption; explicit resume prevents hidden replay"},
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, RemoteFailure) else "worker_error"
                message = (
                    exc.message
                    if isinstance(exc, RemoteFailure)
                    else "Run stopped; operator can inspect correlated logs"
                )
                row["state"]["error"] = {
                    "code": code,
                    "message": message,
                    "exception_type": type(exc).__name__,
                    "elapsed_seconds": time.monotonic() - started,
                }
                await self.finish(row, row["state"], "failed", "run.failed", row["state"]["error"])
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def step(self, row):
        if row["state"]["config"]["mode"] != "spoken":
            return await self._step(row)
        row = {**row, "playback_stream_id": str(uuid4())}
        task = asyncio.create_task(self._step(row))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if not task.done() and not await self.store.current(row):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await self.store.playback(
                        row,
                        {
                            "type": "audio.stop",
                            "stream_id": row["playback_stream_id"],
                            "reason": "step_superseded",
                            "barge_in": True,
                        },
                    )
                    return
            return await task
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.store.playback(
                    row, {"type": "audio.stop", "stream_id": row["playback_stream_id"], "reason": "step_ended"}
                )
            raise

    async def _step(self, row):
        state, config = row["state"], row["state"]["config"]
        token = self.store.cipher.decrypt(bytes(row["credential_ciphertext"])).decode()
        root = self.settings.gateway_url.rstrip("/") + "/v1/mindeval"
        if state["registration"] is None:
            state["registration"] = await json_call(
                self.client,
                "POST",
                f"{root}/runs/{row['id']}/register",
                token,
                body={
                    "profile_ids": config["profile_ids"],
                    "patient_model": config["patient_model"],
                    "clinician_models": config["clinician_models"],
                },
            )
            state["profile"] = await json_call(self.client, "GET", f"{root}/profiles/{config['profile_id']}", token)
            await self.store.finish_step(row, state, "queued", "run.registered", state["registration"])
            return
        if len(state["transcript"]) - 1 >= config["max_turns"] * 2:
            result = await json_call(
                self.client,
                "POST",
                f"{root}/judgments",
                token,
                body={
                    "run_id": str(row["id"]),
                    "profile_id": config["profile_id"],
                    "clinician_model": config["clinician_model"],
                    "interaction": judge_interaction(state),
                    "max_completion_tokens": 8192,
                },
            )
            if not result.get("judgment"):
                raise RemoteFailure("invalid_judgment", "Judge returned no valid five-criterion judgment")
            state["judgment"] = result
            if self.settings.mindguard_model:
                try:
                    state["classification"] = await json_call(
                        self.client,
                        "POST",
                        self.settings.platform_url.rstrip("/") + "/v1/mindguard/assess",
                        token,
                        host=urlsplit(self.settings.public_origin).netloc,
                        body={
                            "model": self.settings.mindguard_model,
                            "messages": judge_interaction(state),
                            "language": config["language"],
                        },
                    )
                except (RemoteFailure, httpx.HTTPError) as exc:
                    # Observer availability must not erase a completed evaluation
                    # or turn absence of an assessment into a "safe" result.
                    state["classification"] = {
                        "model_id": self.settings.mindguard_model,
                        "status": "unavailable",
                        "enforcement": "observe",
                        "error": {
                            "code": getattr(exc, "code", "transport_error"),
                            "http_status": getattr(exc, "status", None),
                            "message": "Observation failed; no safety classification is available",
                        },
                    }
            state["benchmark_eligible"] = config["mode"] == "canonical" and not state["intervened"]
            await self.store.finish_step(
                row,
                state,
                "completed",
                "run.completed",
                {"judgment": result, "intervened": state["intervened"], "mode": config["mode"]},
            )
            return
        role = state["next_role"]
        if state.get("takeover_role") == role:
            await self.store.finish_step(row, state, "takeover", "run.awaiting_human", {"role": role})
            return
        result = await json_call(
            self.client,
            "POST",
            f"{root}/completions",
            token,
            body={
                "run_id": str(row["id"]),
                "profile_id": config["profile_id"],
                "role": role,
                "model": config[f"{role}_model"],
                "messages": messages_for(state, role),
                "temperature": config["temperature"],
                "max_completion_tokens": config["max_completion_tokens"],
            },
        )
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RemoteFailure("empty_completion", "Model returned no visible response")
        turn = {
            "role": role,
            "content": content,
            "source": "model",
            "at": datetime.now(UTC).isoformat(),
            "completion": result,
            "human": False,
        }
        audio_record = None
        if config["mode"] == "spoken":
            turn_index = len(state["transcript"])

            async def emit(event):
                await self.store.playback(
                    row,
                    {
                        **event,
                        "stream_id": row["playback_stream_id"],
                        "turn_index": turn_index,
                        "role": role,
                    },
                )

            async def retain(segment_index, wav, metadata):
                record = await self.store.retain_segment(row, turn_index, segment_index, wav, metadata)
                if record is None:
                    raise RemoteFailure("step_superseded", "Run changed before the speech segment was retained")
                return record

            _, recognized, metadata = await self.speak_and_listen(
                token, content, role, config, emit=emit, retain=retain
            )
            turn["generated_content"], turn["content"], turn["speech"] = content, recognized, metadata
            turn["audio_segments"] = metadata["recordings"]
            if len(turn["audio_segments"]) == 1:
                turn["audio_url"] = turn["audio_segments"][0]["audio_url"]
        state["transcript"].append(turn)
        state["next_role"] = "patient" if role == "clinician" else "clinician"
        state["pending_nudges"] = [n for n in state["pending_nudges"] if n["role"] != role]
        next_status = "takeover" if state.get("takeover_role") == state["next_role"] else "queued"
        await self.store.finish_step(row, state, next_status, "turn.completed", turn, audio=audio_record)

    async def speak_and_listen(self, token, text, role, config, *, emit=None, retain=None):
        headers = {"Authorization": f"Bearer {token}", "Host": urlsplit(self.settings.public_origin).netloc}
        started, first_audio, pcm, rate, final = time.monotonic(), None, bytearray(), None, None
        sequence, segments, recognized, recordings = 0, [], [], []
        duration, pacing, playback_end, single_wav = 0.0, 0.0, 0.0, None
        single_segment = len(speech_segments(text, 1024)) == 1
        if emit:
            await emit({"type": "audio.start", "voice": config[f"{role}_voice"], "mode": "spoken_experience"})
        async for event in synthesis_events(
            self.client,
            self.settings.platform_url.rstrip("/") + "/v1/voice/synthesize",
            headers,
            text,
            role,
            config,
        ):
            if event["type"] == "audio.chunk":
                if first_audio is None:
                    first_audio = time.monotonic() - started
                current_rate = int(event["sample_rate_hz"])
                if not 8000 <= current_rate <= 96000 or event.get("encoding", "pcm_s16le") != "pcm_s16le":
                    raise RemoteFailure("tts_format_invalid", "Speech must be mono PCM16 at a supported sample rate")
                if event.get("channels", 1) != 1:
                    raise RemoteFailure("tts_format_invalid", "Speech must be mono PCM16")
                if rate is not None and rate != current_rate:
                    raise RemoteFailure("tts_format_changed", "Speech sample rate changed within one utterance")
                rate = current_rate
                chunk = base64.b64decode(event["audio_base64"], validate=True)
                if len(chunk) % 2:
                    raise RemoteFailure("tts_format_invalid", "PCM16 speech chunk ended within a sample")
                pcm.extend(chunk)
                if len(pcm) > 8 * 1024 * 1024 - 44:
                    raise RemoteFailure("tts_too_large", "One speech segment exceeded 8 MiB; no audio was truncated")
                if emit:
                    # 2 KiB PCM becomes 2732 base64 bytes, leaving room for the
                    # envelope below the 4 KiB notification boundary.
                    for offset in range(0, len(chunk), 2048):
                        piece = chunk[offset : offset + 2048]
                        await emit(
                            {
                                "type": "audio.chunk",
                                "sequence": sequence,
                                "encoding": "pcm_s16le",
                                "sample_rate_hz": rate,
                                "channels": 1,
                                "audio_base64": base64.b64encode(piece).decode(),
                            }
                        )
                        playback_end = max(playback_end, time.monotonic()) + len(piece) / (rate * 2)
                        sequence += 1
            elif event["type"] == "audio.done":
                final = event
                if not pcm or not rate:
                    raise RemoteFailure("tts_incomplete", "Speech segment contained no audio")
                segment_duration = len(pcm) / (rate * 2)
                audio = wav_bytes(bytes(pcm), rate)
                pcm = bytearray()
                chunk = b""
                result = await self.transcribe(headers, audio, config["language"])
                segment_metadata = {
                    "tts": event,
                    "asr": result,
                    "voice": config[f"{role}_voice"],
                    "duration_seconds": segment_duration,
                    "sample_rate_hz": rate,
                }
                if retain:
                    recordings.append(await retain(event["segment_index"], audio, segment_metadata))
                if single_segment and not retain:
                    single_wav = audio
                del audio
                recognized.append(result["text"].strip())
                segments.append(segment_metadata)
                duration += segment_duration
                # Pace each bounded segment, not an entire long turn, so browser
                # buffering and worker memory do not grow with turn length.
                pause = max(0, playback_end - time.monotonic()) if emit else 0
                if pause:
                    pacing += pause
                    await asyncio.sleep(pause)
            elif event["type"] in {"audio.error", "error"}:
                raise RemoteFailure("tts_failed", "Speech generation returned an error")
        if not recognized or pcm or not rate or final is None:
            raise RemoteFailure("tts_incomplete", "Speech generation did not deliver complete audio")
        if emit:
            await emit({"type": "audio.end", "chunks": sequence, "duration_seconds": duration})
        transcript = " ".join(recognized)
        return (
            single_wav,
            transcript,
            {
                "tts": final,
                "tts_segments": segments,
                "asr": {"text": transcript, "segment_count": len(segments)},
                "recordings": recordings,
                "voice": config[f"{role}_voice"],
                "first_audio_seconds": first_audio,
                "duration_seconds": duration,
                "playback_pacing_seconds": pacing,
                "live_chunks": sequence,
                "experience_mode": "spoken_not_canonical",
                "roundtrip_seconds": time.monotonic() - started,
                "segmentation": "lossless_sentence_then_word_1024_characters",
            },
        )

    async def transcribe(self, headers, audio, language):
        asr_model = "nemotron-speech-en-0-6b" if language == "en" else "nemotron-speech-multilingual-0-6b"
        response = await self.client.post(
            self.settings.platform_url.rstrip("/") + "/v1/audio/transcriptions",
            headers=headers,
            data={"model": asr_model},
            files={"file": ("utterance.wav", audio, "audio/wav")},
        )
        if response.status_code == 202:
            location = response.headers.get("location", "")
            if not location.startswith("/v1/operations/") or "?" in location or ".." in location:
                raise RemoteFailure("asr_invalid_operation", "Speech recognition returned no valid operation location")
            async with asyncio.timeout(600):
                while True:
                    await asyncio.sleep(1)
                    pending = await self.client.get(self.settings.platform_url.rstrip("/") + location, headers=headers)
                    if not pending.is_success:
                        raise RemoteFailure("asr_poll_failed", f"Speech status returned HTTP {pending.status_code}")
                    status = pending.json().get("status")
                    if status == "succeeded":
                        response = await self.client.get(
                            self.settings.platform_url.rstrip("/") + location + "/result", headers=headers
                        )
                        break
                    if status in {"failed", "cancelled", "expired"}:
                        raise RemoteFailure("asr_operation_failed", f"Speech recognition operation {status}")
        if response.status_code != 200:
            raise RemoteFailure("asr_http_error", f"Speech recognition returned HTTP {response.status_code}")
        result = response.json()
        if not result.get("text", "").strip():
            raise RemoteFailure("asr_empty", "Speech recognition produced an empty transcript")
        return result
