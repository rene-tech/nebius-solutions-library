"""Synthetic multilingual and two-speaker/noisy-room acceptance, not clinical validation."""

import argparse
import asyncio
import itertools
import json
import re
import wave
from pathlib import Path

import httpx
import numpy as np

from probe import PARAKEET, SORTFORMER, TEXT, read_pcm, stream, synth

LANGUAGES = {
    "ar": "صباح الخير. يبدأ الاجتماع الساعة التاسعة.",
    "de": "Guten Morgen. Der Workshop beginnt um neun Uhr.",
    "en": "Good morning. The workshop starts at nine.",
    "es": "Buenos días. La reunión comienza a las nueve.",
    "fr": "Bonjour. La réunion commence à neuf heures.",
    "hi": "सुप्रभात। बैठक नौ बजे शुरू होती है।",
    "it": "Buongiorno. La riunione inizia alle nove.",
    "ja": "おはようございます。会議は九時に始まります。",
    "ko": "안녕하세요. 회의는 아홉 시에 시작합니다.",
    "pt": "Bom dia. A reunião começa às nove horas.",
    "vi": "Chào buổi sáng. Cuộc họp bắt đầu lúc chín giờ.",
    "zh": "早上好。会议九点开始。",
}


def wer(reference, actual):
    ref, hyp = (re.findall(r"\w+", text.casefold()) for text in (reference, actual))
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        current = [i]
        for j, candidate in enumerate(hyp, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (word != candidate),
                )
            )
        previous = current
    return previous[-1] / len(ref)


def attribution(events, intervals):
    rows = []
    for event in events:
        if event["type"] != "speaker.activity":
            continue
        for index, probabilities in enumerate(event["probabilities"]):
            timestamp = (
                event["start_seconds"] + (index + 0.5) * event["frame_duration_seconds"]
            )
            expected = next(
                (
                    speaker
                    for start, end, speaker in intervals
                    if start + 0.5 <= timestamp < end - 0.5
                ),
                None,
            )
            if expected is not None:
                predicted = (
                    int(np.argmax(probabilities)) if max(probabilities) >= 0.5 else -1
                )
                rows.append((expected, predicted))
    if not rows:
        raise ValueError("no speaker frames")
    # Anonymous labels: choose best one-to-one permutation, not person identity.
    best = min(
        sum(pred != mapping[truth] for truth, pred in rows)
        for mapping in itertools.permutations(range(4), 2)
    )
    return {
        "scored_speech_frames": len(rows),
        "best_permutation_frame_error_rate": best / len(rows),
        "active_labels": sorted({pred for _, pred in rows if pred >= 0}),
        "boundary_exclusion_seconds": 0.5,
        "is_official_der": False,
    }


async def main(args):
    root, fixtures = Path(args.output), Path(args.fixtures)
    root.mkdir(parents=True, exist_ok=True)
    results = {
        "scope": "synthetic multilingual and two-speaker noise; no clinical claim",
        "languages": [],
    }
    async with httpx.AsyncClient(timeout=240, trust_env=False) as client:
        for language, text in LANGUAGES.items():
            row = await synth(
                client, args.magpie, root / (language + ".wav"), "Sofia", text, language
            )
            results["languages"].append(row)
            (root / "quality-results.json").write_text(
                json.dumps(results, indent=2, ensure_ascii=False) + "\n"
            )
            print(
                json.dumps(
                    {
                        "language": language,
                        "audio_seconds": row["audio_seconds"],
                        "first_audio_seconds": row["first_audio_seconds"],
                    }
                ),
                flush=True,
            )
    samples, intervals = [], []
    offset = 0
    for name, speaker in (("Sofia-4.wav", 0), ("Jason-1.wav", 1), ("Sofia-4.wav", 0)):
        # read_pcm appends one second of silence; include that between turns.
        audio = (
            np.frombuffer(read_pcm(fixtures / name), dtype="<i2").astype(np.float32)
            / 32768
        )
        intervals.append(
            (offset / 16000, (offset + len(audio) - 16000) / 16000, speaker)
        )
        samples.append(audio)
        offset += len(audio)
    original = np.concatenate(samples)
    results["reference_intervals"] = intervals
    results["speaker_cases"] = []
    for noisy in (False, True):
        audio = original.copy()
        if noisy:
            for delay, gain in ((480, 0.25), (1600, 0.12), (4000, 0.05)):
                audio[delay:] += original[:-delay] * gain
            rms = float(np.sqrt(np.mean(audio**2)))
            audio += np.random.default_rng(20260916).normal(
                0, rms / 10 ** (12 / 20), len(audio)
            )
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        name = "two-speaker-room-12db.wav" if noisy else "two-speaker-clean.wav"
        with wave.open(str(root / name), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(pcm)
        asr = await stream(args.parakeet, PARAKEET, pcm)
        text = " ".join(
            e["text"] for e in asr["events"] if e["type"] == "transcript.final"
        )
        diar = await stream(args.sortformer, SORTFORMER, pcm)
        row = {
            "fixture": name,
            "noisy": noisy,
            "text": text,
            "normalized_wer": wer(" ".join([TEXT] * 3), text),
            "parakeet": asr,
            "sortformer": diar,
            "speaker_score": attribution(diar["events"], intervals),
        }
        results["speaker_cases"].append(row)
        print(
            json.dumps(
                {
                    "fixture": name,
                    "wer": row["normalized_wer"],
                    "speaker_score": row["speaker_score"],
                }
            ),
            flush=True,
        )
        (root / "quality-results.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False) + "\n"
        )
    results["status"] = "completed"
    (root / "quality-results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("magpie", "parakeet", "sortformer", "fixtures", "output"):
        parser.add_argument("--" + key, required=True)
    asyncio.run(main(parser.parse_args()))
