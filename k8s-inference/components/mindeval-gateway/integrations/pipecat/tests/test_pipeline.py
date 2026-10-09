import asyncio
import json
from pathlib import Path

import httpx
import pytest

from adapters import MAGPIE, PlatformClient, wav_bytes, word_error_rate
from example import CRITERIA, run_pipeline, validate_wav

VOICES = json.loads((Path(__file__).parent / "two_voices.json").read_text())


class Backend:
    def __init__(self, bad_audio=False, stt_pending=False, bad_judge=False, long_text=False):
        self.calls = []
        self.bad_audio, self.stt_pending = bad_audio, stt_pending
        self.bad_judge = bad_judge
        self.long_text = long_text

    def __call__(self, request):
        assert request.headers["Authorization"] == "Bearer ordinary-platform-token"
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/register"):
            return httpx.Response(200, json=json.loads(request.content))
        if "/profiles/" in path:
            return httpx.Response(
                200,
                json={
                    "id": "profile-000",
                    "patient_system_prompt": "original patient",
                    "clinician_system_prompt": "original clinician",
                },
            )
        if path.endswith("/completions"):
            body = json.loads(request.content)
            voice = "Sofia" if body["role"] == "patient" else "Jason"
            assert body["messages"][0]["content"] == f"original {body['role']}"
            if body["role"] == "patient":
                assert body["messages"][1] == {"role": "assistant", "content": "Hello"}
            return httpx.Response(
                200,
                json={
                    "content": "x" * 4097 if self.long_text else VOICES[voice]["text"],
                    "finish_reason": "stop",
                    "usage": {"total_tokens": 10},
                    "telemetry": {"queue_ms": 1},
                },
            )
        if path.endswith("/synthesize"):
            body = json.loads(request.content)
            assert body["model"] == "magpie-tts-multilingual-357m"
            assert body["text"] == VOICES[body["voice"]]["text"]
            events = [
                {"type": "operation.queued", "operation_id": "fixture"},
                {
                    "type": "audio.start",
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 22050,
                    "channels": 1,
                    "model": MAGPIE,
                    "voice": body["voice"],
                },
                {
                    "type": "audio.chunk",
                    "sample_rate_hz": 22050,
                    "sequence": 0,
                    "audio_base64": VOICES[body["voice"]]["pcm_base64"],
                },
                {"type": "audio.done", "samples": 2, "chunks": 1},
            ]
            if self.bad_audio:
                events.pop()
            return httpx.Response(
                200, text="\n".join(json.dumps(e) for e in events), headers={"Content-Type": "application/x-ndjson"}
            )
        if path.endswith("/transcriptions"):
            assert b"RIFF" in request.content and b"nemotron-speech-en-0-6b" in request.content
            if self.stt_pending:
                return httpx.Response(202, headers={"Location": "/v1/operations/11111111-1111-1111-1111-111111111111"})
            return httpx.Response(200, json={"text": "recognized complete utterance"})
        if "/operations/" in path:
            return httpx.Response(
                200,
                json={"text": "recognized queued utterance"} if path.endswith("/result") else {"status": "succeeded"},
            )
        if path.endswith("/judgments"):
            assert len(json.loads(request.content)["interaction"]) == 3
            return httpx.Response(200, json={"judgment": {} if self.bad_judge else dict.fromkeys(CRITERIA, 4)})
        raise AssertionError(path)


@pytest.mark.parametrize("queued", [False, True])
async def test_real_pipecat_worker_rtvi_handshake_and_two_voice_frames(queued):
    backend = Backend(stt_pending=queued)
    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(backend)) as client:
        report, audio = await asyncio.wait_for(
            run_pipeline(
                PlatformClient(client, "ordinary-platform-token"),
                run_id="test-pipecat",
                profile_id="profile-000",
                patient_model="patient",
                clinician_model="clinician",
            ),
            15,
        )
    assert report["passed"], report["failures"]
    assert report["pipecat_version"] == "1.10.0"
    assert [item["voice"] for item in audio] == ["Jason", "Sofia"]
    assert all(item["pcm"] for item in audio)
    assert len(report["speech_observations"]) == 2
    assert {"TTSAudioRawFrame", "TranscriptionFrame", "MetricsFrame"} <= set(report["frame_types"])
    types = {message["type"] for message in report["rtvi_messages"]}
    assert {"bot-ready", "bot-llm-text", "metrics", "server-message"} <= types
    assert report["canonical_judgment"]
    assert len([r for r in backend.calls if r.url.path.endswith("/transcriptions")]) == 2


def test_wav_output_decodes_with_nonzero_pcm_and_expected_duration():
    properties = validate_wav(wav_bytes(b"\x01\x00\x02\x00", 22050))
    assert properties["samples"] == 2 and properties["peak_pcm16"] == 2
    assert properties["duration_seconds"] == 2 / 22050
    with pytest.raises(ValueError, match="silent"):
        validate_wav(wav_bytes(b"\x00\x00", 22050))


def test_word_error_rate_is_edit_distance_not_semantic_accuracy():
    assert word_error_rate("Hello, friend!", "hello friend") == 0
    assert word_error_rate("one two three four", "one two four") == 0.25
    assert word_error_rate("", "hallucinated words") is None


async def test_truncated_ndjson_cannot_report_success_or_run_judge():
    backend = Backend(bad_audio=True)
    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(backend)) as client:
        report, _ = await asyncio.wait_for(
            run_pipeline(
                PlatformClient(client, "ordinary-platform-token"),
                run_id="bad-stream",
                profile_id="profile-000",
                patient_model="patient",
                clinician_model="clinician",
            ),
            15,
        )
    assert not report["passed"]
    assert "without audio.done" in report["failures"][0]["message"]
    assert report["canonical_judgment"] is None


async def test_stt_rejects_cross_origin_poll_and_does_not_resubmit():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(202, headers={"Location": "https://other.invalid/secret"})

    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(response)) as client:
        with pytest.raises(RuntimeError, match="invalid operation location"):
            await PlatformClient(client, "ordinary-platform-token").transcribe(b"\x00\x00", 16000)
    assert len(requests) == 1


async def test_invalid_judgment_is_not_a_success():
    backend = Backend(bad_judge=True)
    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(backend)) as client:
        report, _ = await run_pipeline(
            PlatformClient(client, "ordinary-platform-token"),
            run_id="bad-judge",
            profile_id="profile-000",
            patient_model="patient",
            clinician_model="clinician",
        )
    assert not report["passed"]
    assert "five valid named criteria" in report["failures"][0]["message"]


async def test_long_text_fails_explicitly_without_truncation_or_voice_request():
    backend = Backend(long_text=True)
    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(backend)) as client:
        report, audio = await run_pipeline(
            PlatformClient(client, "ordinary-platform-token"),
            run_id="long-text",
            profile_id="profile-000",
            patient_model="patient",
            clinician_model="clinician",
        )
    assert not report["passed"] and not audio
    assert "4096" in report["failures"][0]["message"]
    assert len(report["transcript"][1]["content"]) == 4097
    assert not any(request.url.path.endswith("/synthesize") for request in backend.calls)


@pytest.mark.parametrize("payload", [b"", b"odd"])
async def test_stt_invalid_pcm_never_reaches_provider(payload):
    def reject(_request):
        raise AssertionError("invalid PCM was sent upstream")

    async with httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(ValueError, match="bounded PCM16"):
            await PlatformClient(client, "ordinary-platform-token").transcribe(payload, 16000)
