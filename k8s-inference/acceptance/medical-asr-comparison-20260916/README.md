# Medical speech-to-text comparison — 2026-09-16

Completed **49 public-API requests**, 2026-09-16 21:39:09–21:55:27 UTC:
18 full English consultation requests and 31 German reference clips. All API
operations completed; 48 returned nonempty transcripts and **one returned empty
text**, retained as a quality failure. No HTTP/operation failures or duration
mismatches occurred. The temporary API key was revoked. Ten scorer/analysis/
evidence unit tests passed, as did receipt/hash/coverage verification.

This is transcription-quality evaluation, not clinical validation, a new model
deployment, a GPU snapshot test, or a streaming-latency benchmark.

## Quality results

Lower word-error rate (WER) is better. These are **first-attempt results on the
same two complete recordings**, not the best of three attempts.

| App | GI consultation WER | Eczema consultation WER | Combined WER | Combined CER | WER without hesitation tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Nemotron English 0.6B | 17.83% | 16.46% | **17.09%** | 11.88% | **16.03%** |
| Nemotron multilingual 0.6B | 21.63% | 19.03% | 20.23% | 14.08% | 19.20% |
| Parakeet Realtime EOU 120M (new) | 23.89% | 21.90% | 22.82% | 15.54% | 18.61% |

The final column is a **post-hoc sensitivity analysis**, not a replacement for
the primary score. Parakeet omits more hesitation words: removing only the same
listed hesitation tokens from both reference and output puts it between the two
Nemotron models. The combined denominator changes from 3,090 to 2,901 words.
This does not repair medication names, numbers, negations or missing content.
The English Nemotron model remains best on both measures in this small sample.

The user's additional **medical technical-correctness** review is documented in
[CLINICAL-FIDELITY.md](CLINICAL-FIDELITY.md): 44 explicitly graded medical-content
checks with literal source/output evidence. English Nemotron preserves 39 clearly,
multilingual 32 and Parakeet 33; Parakeet also loses a reliable tablet quantity.
These are descriptive agent-reviewed checklist counts, not clinical accuracy
percentages or clinician validation.

### Repeats and observed processing time

Each English model/recording pair ran three times. Both Nemotron models returned
identical text across their repeats. Parakeet returned three slightly different
transcripts per recording: GI WER 23.68–23.89%, eczema WER 21.84–22.08%.
The cause of that variability was not established by this experiment.

| App | 7m38s GI audio: median API wall time (range) | 9m19s eczema audio: median API wall time (range) |
| --- | ---: | ---: |
| Nemotron English | 26.86s (26.56–27.03s) | 31.83s (31.02–32.57s) |
| Nemotron multilingual | 26.61s (26.56–27.51s) | 32.53s (31.43–32.58s) |
| Parakeet | 144.88s (144.85–145.17s) | 176.62s (175.46–176.94s) |

Parakeet's full-file path is about 5.4–5.5 times slower than English Nemotron
**in these deployed configurations**. It runs on L40S, while the Nemotron workers
run on H100; frame sizes also differ. This is not a controlled same-GPU speed
comparison, and it does not establish which part of the difference is the model,
hardware, chunking or serving implementation. All six English model/case pairs
processed these files faster than their recording duration.

### Medical-content examples

Exact first-attempt output excerpts are retained below. They are transcript
comparisons, **not treatment instructions** or a formal clinical error-rate score.

| Reference phrase | Nemotron English | Nemotron multilingual | Parakeet |
| --- | --- | --- | --- |
| gastroenteritis | gastroenteritis | gastroenteritis | gastropentaritis |
| paracetamol … two tablets up to four times a day | paracetamol two tablets up to four times a day | paracetamol uh two tables up to four times a day | parasetamol tube it's up to four times a day |
| loratadine or Piriton | latidine or puritan | loratidine or piritin | la ratadine or piritin |

Parakeet also gives `amoleins` for emollients and `fx ephenidine` for
fexofenadine. English Nemotron is not error-free: it gives `ammoience` for one
emollients occurrence and `stirrid` for one steroid occurrence. These examples
show why a lower overall WER is insufficient for accepting a medical report.
All three preserve several explicit denials, including the initial no-blood
answer and the allergy denial. Alignment flags on repeated conversational `no`
are not proof of a reversed medical fact; do not count them as such without
context/listening adjudication. The complete decoded audio duration was checked,
but that alone does not establish that every spoken fact was captured.

### German recheck

Nemotron multilingual returns **18.31% WER, 12.65% CER** on the fixed 31-clip
subset: 60 substitutions, 58 deletions and 36 insertions over 841 words. This
matches the previous same-subset 154 errors; **all 31 transcript strings exactly
match the historical run**. It must not be directly compared
with the earlier **16.94% WER across all 1,091 clips**, a different corpus.

Row193 again has an empty transcript despite platform operation status
`succeeded`. The benchmark classifies it as `empty-transcript`, retains it, and
scores all nine reference words as deletions. This sample includes music and
short speech; the present experiment does not establish the cause. A successful
HTTP/operation result is not sufficient evidence of successful recognition.
Parakeet and English Nemotron are not scored on unsupported German input.

### Recommendation and limits

Keep English Nemotron as the current choice for these **English recorded
consultations**. Parakeet is not a demonstrated quality upgrade here. Its
end-of-utterance behavior may serve a different interactive need, but this
full-file test does not evaluate that benefit or live partial-transcript latency.
For German, multilingual Nemotron remains the only applicable model in this
comparison, with the empty-output case explicitly unresolved.

None of these measurements establishes readiness for unsupervised medical
documentation. This is only two English simulated consultations and one fixed
German subset, not patient-level clinical validation. No downstream report
generation, diagnosis scoring, accent/noise stratification, model tuning,
same-GPU comparison, snapshot restore or capacity/load test is claimed.

## Scope and method

The new ASR App is **Parakeet Realtime EOU 120M** (English only). Compare it with
the deployed **Nemotron Speech English 0.6B** and **Nemotron Speech Multilingual
0.6B**. Magpie is text-to-speech and Sortformer is diarization: neither is an ASR
competitor and neither gets a fabricated word-accuracy score.

Use both original complete English medical role-play consultations: gastrointestinal
symptoms (457.92s) and eczema (559.20s), 16m57.12s unique audio, 3,090 reference
words. Each model/case runs three times; repeats are not six independent patients.
Doctor/patient human TextGrid turns are ordered by onset, as in the previous
evaluation. Overlap and uncertain reference annotations limit the interpretation
of these approximate mixed-conversation WER values.

All models use the same ordinary public native API, finalized tenant-owned audio
artifacts, and async operation/result endpoints at `https://89.169.99.188` with
normal TLS verification. The original PCM is losslessly FLAC-encoded, never cut
or resampled again. Input hashes and complete decoded duration are checked. All
model output remains uncorrected; empty outputs and failures remain visible.

Primary normalization is unchanged: NFKC/lowercase, remove XML annotation tags,
ignore punctuation, retain hesitations, numbers and spelling. Corpus WER uses
each recording's **first attempt**; all repeats and variability are separately
retained. CER uses the same normalized text without spaces. A disclosed post-hoc
sensitivity analysis removes only the same hesitation tokens from both sides;
it never removes negations, numeric quantities or medication words.

German: only the multilingual App is applicable. Re-run the previous report-quality
selection from MultiMed German/test: `floor(i*1090/29)` for `i=0..29`, plus the
known empty-output challenge at row193. This is 31 clips, 388.70s and 841 reference
words, **not a new run of the 1,091-clip full split**. The same historical subset
scored 154 errors / 841 words = 18.3115% WER. Some clips include music/nonmedical
speech; this is not a uniform clinical benchmark. The long German HHU role-plays
have no verified human transcript and are not assigned invented accuracy scores.

## Execution and hardware

At most two simultaneous operations overall, one per model. Existing production
models were resident; first requests are retained without a special warmup. The
platform can scale its normal Apps during the test. No node-group, quota, model,
replica, decoding, snapshot or infrastructure settings are changed by the driver.
Request-induced autoscaling is observed separately, not reported as fixed-capacity
performance. Timings are customer API invoke-to-complete-result wall times,
excluding artifact upload and local scoring; they are not pure kernel timings or
new-node cold start.

| App | Observed GPU | Driver | Runtime image digest |
| --- | --- | --- | --- |
| Parakeet EOU 120M | NVIDIA L40S 48GB | 580.173.02 | `99c38f2fa6fc7ec267c8475cc25380a98ac6e2b0eb2141b5628569c8b45f98fa` |
| Nemotron English | NVIDIA H100 80GB HBM3 | 580.159.04 | `288f6caf0c2feb3690c963969e4a26c45a38ca1338f4abe80570b24db91423d2` |
| Nemotron multilingual | NVIDIA H100 80GB HBM3 | 580.159.04 | `628c1ffdb68c6e510de5a691432b4da9a6f64941ce5f35c57213ad8cea8b7205` |

These are **as-deployed configurations**, not a same-GPU model-efficiency
comparison. Parakeet uses 80ms cache-aware frames; the Nemotron profiles use
560ms chunks. Worker Pod/node/GPU identities where provided, preemptibility, model revisions,
operation timing, usage accounting and raw result receipts are retained in
`results-r1/`. **Nineteen Nemotron operation receipts omit runtime identities**:
2/6 English and 17/37 multilingual. They report empty GPU lists/count0 and null
Pod/node/preemptibility, not evidence of CPU execution or zero GPU cost. The
remaining 30 requests identify the observed resident workers. This attribution
gap limits exact per-request hardware/accounting claims; it is not silently
filled in. Live `nvidia-smi` evidence is preserved in `hardware.json`.
The apparent `cold_start_seconds` in an operation is activation
bookkeeping here, not evidence of a cold boot or snapshot restore.

## Reproduction and artifacts

Driver source: `53721fb03b212c9c8b45826e375e260b4e89b7f5`, based on integration
`672177370`; analysis/tests: `78fe1b270`, receipt verification and expanded
term flags: `4d83ba091`; medical-content review source: `8175ed022`.
The same code is published in integration commit
[`4afa2e5bd`](https://github.com/rene-tech/nebius-solutions-library/commit/4afa2e5bd).
Run from that benchmark/integration source, which includes the existing Nemotron
artifact helpers and scorer. The report-only copy on main intentionally does
not merge unrelated, in-progress workshop/model deployment code.
PEP723 dependencies pin httpx0.28.1, pyarrow21.0.0 and rapidfuzz3.14.1.

```bash
uv run --script k8s-inference/acceptance/medical-asr-comparison-20260916/benchmark.py \
  --kubeconfig /path/to/authorized/kubeconfig \
  --assets /home/tux/demo-assets/medical-speech-en-de-20260916 \
  --output /path/to/new-results-directory --german-subset
```

- `run.json`: corpus, source hashes, model/source pins, worker identity and cleanup.
- `<model>-<case>-rN.json`: operation receipt, full transcript, WER/CER, every word
  alignment error, sample counts, duration and timing. Pending/error receipts are
  saved too; HTTP200 alone is not a quality pass.
- `*.result.json.gz`: complete native result, including streaming-derived events;
  no original audio is embedded in Git.
- `summary.json`: per-case repetitions and first-attempt unique-corpus aggregates.
- `medical-details.json`: term/number/negation alignment flags and hesitation-normalized
  scores. Flags are not clinical fact judgments; inspect surrounding text.
- `verification.json`: complete expected-request coverage, raw-result hashes,
  duration checks, historical German transcript comparison and runtime identities.
- `clinical-fidelity.json` and `CLINICAL-FIDELITY.md`: medical-content judgments,
  literal evidence/hashes, German examples and reference-note discrepancies.
- A short-lived Rene test key is created only for these three Apps and revoked in
  `finally`. No key, bearer or signed storage URL is retained. Normal platform
  request/input/output artifacts remain under Rene's existing retention policy;
  temporary local encoded audio is removed automatically.

Attribution: English PriMock57, Babylon Health, CC BY4.0, revision
`cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5`; doctor/patient channels mixed at 0.5 each
without cuts. German MultiMed (`leduckhai/MultiMed`, publisher license label MIT),
revision `459d0ab6db332904f9d7b76a8baabf3333958fa8`; parquet SHA256
`494a635916ceaed914f6238fb7acf37e38a1e8432c30663a2f6f484dbdec58e0`.
The user-provided asset bundle retains the original license/source material.

Task Deck: `fs2-medical-asr-parakeet-comparison-r20260916` under NIM Fast Start Platform.

## Offline verification

From the integration source above, with this report/results directory present:

```bash
python3 k8s-inference/acceptance/medical-asr-comparison-20260916/analyze.py \
  k8s-inference/acceptance/medical-asr-comparison-20260916/results-r1 \
  --output k8s-inference/acceptance/medical-asr-comparison-20260916/summary.json
python3 k8s-inference/acceptance/medical-asr-comparison-20260916/medical_details.py \
  k8s-inference/acceptance/medical-asr-comparison-20260916/results-r1 \
  --output k8s-inference/acceptance/medical-asr-comparison-20260916/medical-details.json
python3 k8s-inference/acceptance/medical-asr-comparison-20260916/verify_results.py \
  k8s-inference/acceptance/medical-asr-comparison-20260916 \
  --historical-german k8s-inference/acceptance/nemotron-speech-20260916/multimed-de-r1.jsonl
python3 k8s-inference/acceptance/medical-asr-comparison-20260916/clinical_fidelity.py \
  k8s-inference/acceptance/medical-asr-comparison-20260916
uv run --with httpx==0.28.1 --with rapidfuzz==3.14.1 python -m unittest discover \
  -s k8s-inference/acceptance/medical-asr-comparison-20260916 -p 'test_*.py' -v
uv run --with pytest==8.4.2 python -m pytest -q \
  k8s-inference/acceptance/nemotron-speech-20260916/test_score_medical.py
```
