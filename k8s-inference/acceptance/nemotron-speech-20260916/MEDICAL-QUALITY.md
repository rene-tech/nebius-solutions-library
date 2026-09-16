# Full medical-consultation diagnostic — 2026-09-16

**Subsequent report-generation evaluation:** [English/German report quality](REPORT-QUALITY.md)
contains seven full ASR-based drafts, English human-reference controls and
paired German excerpt notes. That later experiment does not change these ASR
scores or constitute clinical validation; historical statements below describe
what had been performed at the time of this cohort.

These are measured direct-runtime results, **not public API acceptance or
clinical qualification**. All five files supplied in
`/home/tux/demo-assets/medical-speech-en-de-20260916/README.md` were decoded in
full. Every prepared sample reached inference; no duration truncation occurred.
Input/checkpoint checksums, full transcripts, alignments and error alignments
are in `medical-r1-summary.json`; complete paced events are in the two raw logs.

## Whole-file results

| Recording | Audio duration | English App processing | Multilingual App processing | Approximate English WER: English / multilingual |
| --- | ---: | ---: | ---: | ---: |
| English consultation 01, gastrointestinal | 457.92 s | 39.60 s | 41.41 s | 17.83% / 21.63% |
| English consultation 02, eczema | 559.20 s | 29.45 s | 29.28 s | 16.46% / 19.03% |
| German Herzrasen | 421.86 s | Not applicable | 21.39 s | Not scored |
| German grippaler Infekt | 629.26 s | Not applicable | 30.41 s | Not scored |
| German Polyarthritis | 438.39 s | Not applicable | 21.03 s | Not scored |

One observation per cell. The first recording includes first-request kernel
work; no synthetic warmup was run before this cohort. These measurements exclude
image pull/model acquisition/load and use existing preemptible H100s, 560 ms,
float32, greedy decoding, word output, one active stream per runtime. A resident
idle diagnostic server also occupied each GPU. They are not clean exclusive-GPU
throughput, startup benchmarks or percentile statistics.

Both medical processes use the pinned NVIDIA NeMo pipeline in image
`8e44d3a277a88ffb25d8656654e5e82a7d9378c75542751ec9faa43a823743bb`,
with the committed `medical_probe.py` uploaded via `PYTHONPATH` and
`TORCHINDUCTOR_COMPILE_THREADS=1`. Public gateway and the later model-specific
offline-weight images were not part of this cohort.

## Real-time-paced results

| Recording / App | First nonempty partial from stream start | First final | EOS finalization | File/live normalized text |
| --- | ---: | ---: | ---: | --- |
| English consultation 01 / English | 3.410 s | 5.650 s | 0.029 s | Identical |
| German Herzrasen / multilingual | 9.015 s | 13.492 s | 0.028 s | Identical |

All audio was sent in 20 ms frames at recording speed, not uploaded first and
replayed as text. First partial is measured from session start, not verified
speech onset. English's first reference turn starts at 2.533s. The German model
aligns its first recognized word at8.400s, suggesting about0.615s of additional
latency. This is **model-derived alignment, not independently verified onset**.
A -40dB detector finds non-silent audio at0.436s, but cannot distinguish music,
speech or noise. Therefore neither “nine seconds of model latency” nor “nine
seconds of silence” is established. A listening/onset check remains necessary
before claiming a speech-onset latency target. Finalization and file/live
consistency passed.

## Quality interpretation

English scoring uses the supplied PriMock57 human TextGrids, with doctor/patient
turns ordered by interval start. Overlapping turns do not have a human-verified
combined word ordering. Scores are therefore approximate mixed-conversation
WER, not a published benchmark score. Normalization is fixed and disclosed in
`score_medical.py`: lowercase/NFKC, remove annotation tags, ignore punctuation,
retain hesitations and literal number/spelling forms. All substitutions,
deletions and insertions are retained, not just a successful-word subset.

The English-only model scores better on both English consultations. Important
terms still have errors: the alignment includes English `steroid` → `stirrid`
and `antihistamines` → `antistamines`; multilingual includes `steroid` →
`sterile` and `diarrhea` → `diary`. Some errors involve overlapping speech or
ambiguous references and require listening review. These examples are enough
to rule out claiming medically reliable transcription from “nonempty output”.
Do not automatically correct medication names or negations and then score the
corrected transcript as model accuracy. Phrase biasing/decoding comparisons
should retain both the original and candidate results on the same corpus.

German has **no verified human transcript** in this bundle. File/live agreement
is reproducibility, not accuracy. The full German transcripts are available for
review, including the original teaching narration; narration must not become
patient findings in a downstream report. No report generation, speaker
diarization validation or clinical review was performed.

A separate human-transcribed German reference set has since been evaluated:
[full MultiMed German test split](GERMAN-MULTIMED.md),1,091clips,16.94%WER and
8.88%CER. This supplements these HHU recordings; it does not create a reference
for them. Subsequent [public integration cohorts](PUBLIC-INTEGRATION.md) retain
full customer-path results and failures separately from private-runtime quality.

## Attribution / reproduction

English: Babylon Health / PriMock57, CC BY 4.0, revision
`cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5`. German: HHU medical communication
teaching recordings, CC BY 3.0 DE. Source URLs, contributors and the uncut
mono16k conversions are retained in the supplied asset README and manifest.
Do not commit the large source audio/video files to this repository.

Run `python -m fs2_speech.medical_probe --model MODEL --assets ASSET_ROOT` inside
the pinned GPU runtime and retain stdout. Score with:

```bash
python score_medical.py --assets /home/tux/demo-assets/medical-speech-en-de-20260916 \
  --logs medical-en-r1.log medical-multi-r1.log --output medical-r1-summary.json
```

Remaining: quality/latency optimization with unchanged comparable cohorts,
German human review/reference, public file/live tests with real tenant grants,
mixed load/scaling/preemption/drain and public cold-start benefit. Subsequent
[fresh snapshot restores](SNAPSHOT-RESTORE.md) passed full-recording equality.
Passing these private diagnostics does not complete onboarding.
