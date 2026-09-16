# German medical transcription benchmark — 2026-09-16

The multilingual Nemotron model achieved **16.94% word error rate (WER)** and
**8.88% character error rate (CER)** on the complete available MultiMed German
test split. It processed approximately 3.8 hours of audio in 12.43 minutes of
worker processing time. This measures transcription quality and warmed private
worker speed, **not customer readiness, clinical accuracy or cold start**.

## Dataset and reproducibility

- Source: [MultiMed dataset](https://huggingface.co/datasets/leduckhai/MultiMed),
  German/test, revision `459d0ab6db332904f9d7b76a8baabf3333958fa8`.
- The [authors' paper](https://aclanthology.org/2025.acl-industry.79/) describes
  human-labeled medical speech. The publisher's dataset card labels the dataset
  MIT and it is not access-gated. This is an internal evaluation, not inclusion
  of source recordings in the customer platform or independent verification of
  third-party source rights. No generated/model transcript was used as truth.
- Full split: 1,091 rows and 1,091 different audio checksums; 1,090 scorable
  references. Row998 contains only an ellipsis: it was run and retained, but
  cannot establish word accuracy. The references contain repeated phrases and
  clips are not necessarily independent speakers/conversations.
- Parquet SHA256:
  `494a635916ceaed914f6238fb7acf37e38a1e8432c30663a2f6f484dbdec58e0`.
- Local source file:
  `/home/tux/demo-assets/medical-speech-en-de-20260916/references/de/multimed/test.parquet`.
  The 137.7MB source audio is not committed. Exact references, recognized text,
  word alignments, errors and source-audio hashes are retained in
  `multimed-de-r1.jsonl` (including every failed/empty case and all repetitions).
- `benchmark_multimed.py` at commit `78e5ef7b1` staged the OGG files temporarily
  in Rene's existing bucket, invoked the existing private `/generate` endpoint,
  then deleted all 1,091 task-owned objects. No credentials or signed URLs are
  in the result files. Scorer: `score_medical.py`; additional summary:
  `analyze_multimed.py`. Dependencies are pinned in the benchmark script.

## Fixed model and hardware

Model: `nvidia/nemotron-3.5-asr-streaming-0.6b`, revision
`ea30d66debe3740a08b573244286791d423d6b3e`.
Public App ID: `nemotron-speech-multilingual-0-6b`; internal worker profile ID
uses `nemotron-speech-multilingual-0.6b`.

One H100 80GB HBM3, SM90, driver580.159.04, NeMo
`3b08b2acacc13ec1268e53653346266202b2335f`; float32, greedy_batch, 560ms chunks,
explicit de-DE, word alignments, stripped language tags, no CUDA Graphs,
one request/session at a time. No medical fine-tuning, phrase biasing or ITN.
Runtime image digest:
`sha256:628c1ffdb68c6e510de5a691432b4da9a6f64941ce5f35c57213ad8cea8b7205`.

Worker `fs2-speech-restore-multi-0916-r2`, UID
`ed05a45a-f00b-4d54-8cba-c3b9d6ccb5b7`, on
`computeinstance-e00fkt1bsa4ec657sn`, was already warmed and GPU-snapshot-restored.
The request driver ran inside the same Pod. Timings exclude queueing, node
provisioning, image pull, model load, restore and the public gateway. No model or
decoding setting changed during the test. See `SNAPSHOT-RESTORE.md` for the
separate clean-capture/restore experiment; this test does not enable snapshots
in production automatically.

## Results

| Measurement | Result |
|---|---:|
| Full-split requests | 1,091 |
| HTTP failures | 0 |
| Reference words | 30,709 |
| Substitutions / deletions / insertions | 2,405 / 1,846 / 950 |
| Word error rate | 16.9364% |
| Character error rate | 8.8834% |
| Decoded audio, including blank-output clips | 13,660.463s (3.795h) |
| Worker processing, including blank-output clips | 745.817s |
| Sum of private HTTP request wall times | 774.726s |
| Processing real-time factor | 0.05460 (18.32× real time) |
| Median full-split private HTTP latency | 0.706s |

The dataset's duration field sums to13,666s; timing uses actual decoded sample
counts, not rounded metadata. The original metadata summary additionally reports
nonempty-output-only duration/processing. `multimed-de-r1-analysis.json` includes
all decoded clips and is the source of the table above.

Thirty fixed evenly spaced rows were then repeated three times (90 additional
requests, not additional quality samples). Each repetition covers378.45s audio.
Their summed private HTTP times were20.934,21.048,20.977s; mean20.986s,
sample standard deviation0.057s. Every repeated transcript exactly matched its
full-pass transcript. These are warm consistency measurements, not a p99/SLA.

## Quality findings and limits

There were three blank outputs. Row992 is a0.5s clip labeled `[Musik]`; row998 is
the6s ellipsis-only reference. Neither establishes a missed spoken word.
**Row193 is a12.92s clip with spoken words/numbers in its supplied reference and
an empty recognized transcript.** It remains in the scored denominator as
deletions; it has not been relabeled as a passing semantic response.

Examples from reference/hypothesis alignments include HIV→HV,
Blutdruck→Blutrug, and Blutdruckmessung→Mitbluck. These illustrate why a low
processing time or HTTP200 does not make this medically reliable. They are
dataset-alignment observations, not independently adjudicated listening tests.

Normalization: NFKC, lowercase, punctuation ignored, XML tags removed. German
umlauts/ß are preserved. Numerals vs written numbers, compound splitting,
hesitations and words inside speaker/music annotations are **not** corrected.
CER uses the same normalization with whitespace removed. These scores are not
directly comparable to published results using a different normalizer, nor to
the earlier English consultation WER on a different corpus.

Next quality experiment: measure documented contextual phrase biasing/decoding
options against this unchanged corpus, retain both quality and latency, and
inspect medically important terms/numbers/negations separately. No claim of
clinical suitability or automatic medical report correctness follows from this
benchmark. The supplied HHU German long recordings still have no verified
reference; MultiMed supplements them rather than inventing one for them.

## Public rollout, kept separate

Speech backend image from6b9fc084d is deployed (Helm127/128), and both speech
Apps were created through the admin preview/apply API with access restricted to
Rene during onboarding, one hot replica and at most two replicas per model.
Existing model registrations and infrastructure limits were preserved.

The first full public multipart cohort failed HTTP500 because Starlette tried
to spool a >1MiB file and the read-only gateway container had no writable `/tmp`.
The negative receipt is `public-medical-files-r1.json`; its temporary key was
revoked. Chart fix6a7c84193 adds bounded Pod-local multipart scratch, with145
Helm/route tests passing. Retest receipts and rollout status must be consulted
before claiming this failure resolved. Public live/MCP, scaling, production
snapshot activation and complete feature acceptance are still separate gates.
