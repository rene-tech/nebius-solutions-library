import io
import sys
import wave

import pytest

from conftest import SOLUTION_ROOT

sys.path.insert(0, str(SOLUTION_ROOT / "acceptance/performance-placement-20260920"))
from extra import validate


@pytest.mark.parametrize("silent", [False, True])
def test_music_validator_requires_complete_nonsilent_requested_duration(tmp_path, silent):
    encoded = io.BytesIO()
    with wave.open(encoded, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes((b"\x00\x00" if silent else b"\x01\x00") * 8000)
    args = ("ace-step-1-5", {"duration_seconds": 1}, {}, encoded.getvalue(), tmp_path, 0)
    if silent:
        with pytest.raises(RuntimeError, match="music_silent_wave"):
            validate(*args)
    else:
        result = validate(*args)
        assert result["duration_seconds"] == 1 and "not-musical-quality" in result["scope"]
        with pytest.raises(RuntimeError, match="music_duration_mismatch"):
            validate("ace-step-1-5", {"duration_seconds": 2}, {}, encoded.getvalue(), tmp_path, 0)
