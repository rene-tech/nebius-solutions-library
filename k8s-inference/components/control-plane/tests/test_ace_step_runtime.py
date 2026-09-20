import struct

import pytest

from fs2_serve.runtime import RuntimeClient, RuntimeProtocolError


def wav(seconds: float = 0.01, rate: int = 48000) -> bytes:
    frames = round(seconds * rate)
    samples = b"\x00\x00" * frames
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


def test_ace_step_wav_validation_and_usage():
    result = RuntimeClient._ace_step_wave_usage(wav(), "audio/wav")
    assert result.modalities[0].modality == "audio"
    assert result.modalities[0].unit == "seconds"
    assert result.modalities[0].amount == pytest.approx(0.01)


@pytest.mark.parametrize("body,media_type", [(b"not-wav", "audio/wav"), (wav(), "application/json")])
def test_ace_step_rejects_invalid_binary(body, media_type):
    with pytest.raises(RuntimeProtocolError):
        RuntimeClient._ace_step_wave_usage(body, media_type)
