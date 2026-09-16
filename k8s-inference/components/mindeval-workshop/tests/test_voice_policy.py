import os

import httpx
import numpy as np
import pytest
from test_api import authentication
from test_playback import Socket
from test_store import IDENTITY, create
from test_worker import SETTINGS

from fs2_workshop.app import create_app
from fs2_workshop.models import Intervention
from fs2_workshop.voice_policy import PARAKEET, SileroCPU, VoicePolicy, clean_text


class Probabilities:
    def __init__(self, values):
        self.values = iter(values)

    def probability(self, samples, state, context):
        assert samples.shape == (512,)
        return next(self.values), state, context


def test_silence_never_creates_a_turn_and_short_pause_does_not_finish():
    policy = VoicePolicy(Probabilities([0] * 100 + [0.9] * 4 + [0] * 20 + [0.9] * 2 + [0] * 38), asr_model=PARAKEET)
    events = [event for _ in range(164) for event in policy.feed(bytes(1024))]
    assert [event["type"] for event in events] == ["voice_policy.speech_start", "voice_policy.speech_end"]
    assert events[-1]["source"] == "silero_vad_silence" and not events[-1]["model_eou"]
    assert policy.finished


def test_eou_eob_and_phrase_candidates_are_distinct_and_never_suppress_text():
    policy = VoicePolicy(None, asr_model=PARAKEET)
    assert not policy.observe({"type": "turn.eou", "source": "silence"})
    phrase = policy.observe({"type": "transcript.final", "text": "Uh-huh!", "segment": 0})[0]
    assert phrase["source"] == "nemo_phrase_rule" and not phrase["model_eob"] and not phrase["suppressed"]
    assert not policy.finished
    assert not policy.observe({"type": "transcript.final", "text": "Uh-huh!", "segment": 0})
    eob = policy.observe({"type": "turn.eob", "source": "model_token", "segment": 0})[0]
    assert eob["model_eob"] and not policy.finished and not eob["automatic_speech"]
    eou = policy.observe({"type": "turn.eou", "source": "model_token", "segment": 0})[0]
    assert eou["model_eou"] and policy.finished
    assert not VoicePolicy(None, asr_model="nemotron-speech-en-0-6b").observe(
        {"type": "turn.eou", "source": "model_token"}
    )
    assert not VoicePolicy(None, asr_model=PARAKEET, language="de").observe(
        {"type": "transcript.final", "text": "okay"}
    )
    assert clean_text("  Yeah, RIGHT!<EOU>") == "yeah right"


def test_pcm_partial_windows_and_state_are_private_to_each_microphone():
    first = VoicePolicy(Probabilities([0.9]), asr_model=PARAKEET)
    second = VoicePolicy(first.model, asr_model=PARAKEET)
    assert not first.feed(bytes(512))
    assert not first.feed(bytes(512))
    assert first.samples == 512 and second.samples == 0
    first.state[0, 0, 0] = 2
    assert np.all(second.state == 0) and not second.pending
    for pcm in (b"", b"x", bytes(32002)):
        with pytest.raises(ValueError):
            second.feed(pcm)


def test_pinned_real_silero_cpu_model_and_no_speech_on_silence(tmp_path):
    path = os.environ.get("WORKSHOP_TEST_SILERO_MODEL")
    if not path:
        pytest.skip("set WORKSHOP_TEST_SILERO_MODEL for pinned checkpoint qualification")
    model = SileroCPU(path)
    assert model.session.get_providers() == ["CPUExecutionProvider"]
    policy = VoicePolicy(model, asr_model=PARAKEET)
    assert not [event for _ in range(64) for event in policy.feed(bytes(1024))]
    invalid = tmp_path / "invalid.onnx"
    invalid.write_bytes(b"not-the-checkpoint")
    with pytest.raises(ValueError, match="checksum"):
        SileroCPU(invalid)


@pytest.mark.parametrize("mode,available", [("canonical", True), ("spoken", False)])
async def test_auto_finish_requires_spoken_mode_and_available_pinned_model(store, mode, available):
    row = await create(store, mode=mode)
    await store.intervene(row["id"], IDENTITY, Intervention(action="takeover", role="clinician"))

    class AutoSocket(Socket):
        async def receive_text(self):
            return '{"token":"team1","auto_finish":true}'

    settings = SETTINGS.model_copy(update={"auth_url": "http://platform/internal/ext-authz"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(authentication)) as client:
        app = create_app(settings, store=store, client=client, start_workers=False)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "").endswith("/microphone"))
        async with app.router.lifespan_context(app):
            app.state.silero = object() if available else None
            browser = AutoSocket("team1")
            await endpoint(browser, row["id"])
            result = await browser.sent.get()
            assert result["type"] == "workshop.error"
            assert ("spoken experience" if mode == "canonical" else "manual Finish") in result["message"]
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_workshop.audio WHERE run_id=$1", row["id"]) == 0
