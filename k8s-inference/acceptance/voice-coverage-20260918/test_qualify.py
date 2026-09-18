import io
import struct
import wave

import pytest

from qualify import diarization_score, intervals, wav_integrity


def result(rows):
    return {"audio_seconds": len(rows)*.08, "events": [{"type": "speaker.activity", "start_seconds": 0.,
            "frame_duration_seconds": .08, "probabilities": rows}]}


def test_global_mapping_overlap_and_silence():
    # Labels differ from reference identity; the second frame has two speakers.
    value = result([[0, 1, 0, 0], [0, 1, 1, 0], [0, 0, 1, 0], [0, 0, 0, 0]])
    score = diarization_score(value, [[(0, .16)], [(.08, .24)]], .32)
    assert score["DER"] == pytest.approx(0)
    assert score["reference_speaker_seconds"] == pytest.approx(.32)
    assert score["reference_to_model_speaker"] == [1, 2]


def test_label_swap_cannot_use_per_frame_oracle():
    score = diarization_score(result([[1, 0, 0, 0], [0, 1, 0, 0]]), [[(0, .16)]], .16)
    assert score["DER"] == pytest.approx(.5)


def test_missing_speech_and_false_alarm_are_separate():
    score = diarization_score(result([[0, 0, 0, 0], [1, 0, 0, 0]]), [[(0, .08)]], .16)
    assert score["DER"] == pytest.approx(2)
    assert score["missed_speaker_seconds"] == pytest.approx(.08)
    assert score["false_alarm_speaker_seconds"] == pytest.approx(.08)


@pytest.mark.parametrize("rows", [[[float("nan"), 0, 0, 0]], [[2, 0, 0, 0]], [[1, 0, 0]]])
def test_invalid_probabilities_rejected(rows):
    with pytest.raises(ValueError):
        diarization_score(result(rows), [[(0, .08)]], .08)


def test_interval_quotes_and_silence(tmp_path):
    source = tmp_path / "source.TextGrid"
    source.write_text('intervals [1]:\n xmin = 0\n xmax = 1\n text = ""\n'
                      'intervals [2]:\n xmin = 1\n xmax = 2\n text = "said ""yes"""')
    assert intervals(source) == [(1., 2., 'said "yes"')]


def test_complete_wave_and_silence():
    def wav(values):
        out = io.BytesIO()
        with wave.open(out, "wb") as audio:
            audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            audio.writeframes(struct.pack("<"+"h"*len(values), *values))
        return out.getvalue()
    evaluation, pcm = wav_integrity(wav([1, -2, 3]))
    assert evaluation["frames"] == 3 and len(pcm) == 6
    with pytest.raises(ValueError, match="Silent"):
        wav_integrity(wav([0, 0]))
