"""One durable turn at a time, with explicit interruption and inference evidence."""

import asyncio
import base64
import contextlib
import io
import json
import time
import wave
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .models import judge_interaction, messages_for


class RemoteFailure(Exception):
    def __init__(self, code, message, status=None):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


async def json_call(client, method, url, token, *, body=None):
    response = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, json=body)
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


class Worker:
    def __init__(self, store, settings, client):
        self.store, self.settings, self.client = store, settings, client
        self.owner = str(uuid4())
        self.stopping = False

    async def heartbeat(self, run_id):
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            await self.store.heartbeat(run_id, self.owner, self.settings.lease_seconds)

    async def loop(self):
        while not self.stopping:
            row = await self.store.claim(self.owner, self.settings.lease_seconds, self.settings.max_team_workers)
            if row is None:
                await asyncio.sleep(0.5)
                continue
            heartbeat = asyncio.create_task(self.heartbeat(row["id"]))
            started = time.monotonic()
            try:
                await self.step(row)
            except asyncio.CancelledError:
                await self.store.finish_step(
                    row,
                    row["state"],
                    "interrupted",
                    "run.interrupted",
                    {"reason": "worker shutdown; resume explicitly"},
                )
                raise
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
                await self.store.finish_step(row, row["state"], "failed", "run.failed", row["state"]["error"])
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def step(self, row):
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
            audio, recognized, metadata = await self.speak_and_listen(token, content, role, config)
            turn["generated_content"], turn["content"], turn["speech"] = content, recognized, metadata
            turn_index = len(state["transcript"])
            audio_record = (turn_index, audio, metadata)
            turn["audio_url"] = f"/v1/workshop/runs/{row['id']}/audio/{turn_index}"
        state["transcript"].append(turn)
        state["next_role"] = "patient" if role == "clinician" else "clinician"
        state["pending_nudges"] = [n for n in state["pending_nudges"] if n["role"] != role]
        next_status = "takeover" if state.get("takeover_role") == state["next_role"] else "queued"
        await self.store.finish_step(row, state, next_status, "turn.completed", turn, audio=audio_record)

    async def speak_and_listen(self, token, text, role, config):
        headers = {"Authorization": f"Bearer {token}", "Host": urlsplit(self.settings.public_origin).netloc}
        started, first_audio, pcm, rate, final = time.monotonic(), None, bytearray(), None, None
        async with self.client.stream(
            "POST",
            self.settings.platform_url.rstrip("/") + "/v1/voice/synthesize",
            headers=headers,
            json={
                "model": "magpie-tts-multilingual-357m",
                "text": text,
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
                    if first_audio is None:
                        first_audio = time.monotonic() - started
                    current_rate = int(event["sample_rate_hz"])
                    if rate is not None and rate != current_rate:
                        raise RemoteFailure("tts_format_changed", "Speech sample rate changed within one utterance")
                    rate = current_rate
                    pcm.extend(base64.b64decode(event["audio_base64"], validate=True))
                    if len(pcm) > 8 * 1024 * 1024 - 44:
                        raise RemoteFailure(
                            "tts_too_large", "Synthesized utterance exceeded the 8 MiB speech upload limit"
                        )
                elif event["type"] == "audio.done":
                    final = event
                elif event["type"] in {"audio.error", "error"}:
                    raise RemoteFailure("tts_failed", "Speech generation returned an error")
        if not pcm or not rate or final is None:
            raise RemoteFailure("tts_incomplete", "Speech generation did not deliver complete audio")
        audio = wav_bytes(bytes(pcm), rate)
        asr_model = "nemotron-speech-en-0-6b" if config["language"] == "en" else "nemotron-speech-multilingual-0-6b"
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
        return (
            audio,
            result["text"],
            {
                "tts": final,
                "asr": result,
                "voice": config[f"{role}_voice"],
                "first_audio_seconds": first_audio,
                "roundtrip_seconds": time.monotonic() - started,
            },
        )
