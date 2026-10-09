"""Small reproducible native protocol fixtures, supplementary to medical cohorts.

These are not substitutes for the full recordings or clinical-quality evidence.
The fixed runtime image supplies espeak-ng and ffmpeg; generated PCM is hashed.
"""

from pathlib import Path
import subprocess

FIXTURES = (
    (
        "speech-fixture-observatory",
        "The research team is testing speech recognition. "
        "Please retain the complete recording, including the final word. The final word is telescope.",
    ),
    (
        "speech-fixture-workshop",
        "This is a different recording for the scientific platform. "
        "We compare complete file transcription with live audio. The final word is microscope.",
    ),
)


def prepare(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for identity, text in FIXTURES:
        raw, prepared = (
            directory / (identity + "-raw.wav"),
            directory / (identity + ".wav"),
        )
        subprocess.run(
            ["espeak-ng", "-v", "en-us", "-s", "145", "-w", str(raw), text], check=True
        )
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(raw),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(prepared),
            ],
            check=True,
        )
        result.append((identity, prepared.name, "en-US"))
    return result
