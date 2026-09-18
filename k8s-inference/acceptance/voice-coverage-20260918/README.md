# Public Magpie / Sortformer qualification

This bounded campaign uses only the assigned scientist09 ordinary key and one
model operation at a time. It reuses the campaign's public MCP and immutable
artifact clients. It makes no LibreChat, medical-accuracy, voice-identity,
all-language, or broad customer-readiness claim.

`qualify.py --prepare` freezes 11 cases: five Magpie named voices with source
English/German text, two phrase-incremental syntheses, two full acted PriMock
consultations, and a matched 60-second native/real-time WebSocket diarization
crop. Run preparation only once for a new output directory. Source audio and
TextGrid reference digests are preserved; the input key is derived from the
frozen complete case. Resume looks up that same key, never invents a replacement
after ambiguous admission, and stops before the next case on harness/transport
uncertainty. Streaming sessions are not replayed after accepted audio.

Required flags are `--campaign` (protected campaign root), `--client-dir`
(existing workbench `scripts/qualification`), `--assets` (original public speech
asset root), and `--output` (task-owned private receipt directory). `--only`
selects existing case IDs without changing identities. Credentials are read in
memory from the existing protected scientist manifest and never printed.

The offline scorer integrates the model's 80ms probabilities at threshold0.5,
with a single global optimal speaker permutation, zero collar, and overlapping
reference speech included. References are the genuine human **utterance-level**
doctor/patient TextGrids, not a perfect voice-activity annotation. Labels are
not identified people. DER is measured and reported, never converted to an
arbitrary success threshold or represented as published benchmark reproduction.
Numeric integrity, duration and timestamp checks remain separate from DER.

`summarize.py --output ... --scorer .../score_medical.py` measures complete WAV
frames/hashes, PCM/retained-WAV equality for streams, first-audio latency, chunk
gaps, durations, near-zero/near-clipping sample fractions, and operation RTF.
RTF includes transport/publication, not isolated GPU compute. It prepares seven
optional ASR readback cases for the existing sequential campaign runner. These
are **dual-model round-trip proxies**, not independently established TTS
intelligibility ground truth. A valid WAV or changed waveform alone does not
establish correct spoken content.

Primary sources:

- [PriMock57 acted conversations](https://github.com/babylonhealth/primock57),
  pinned campaign revision `cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5`, CC-BY-4.0.
- [Sortformer model card](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1).
- [Magpie model card](https://huggingface.co/nvidia/magpie_tts_multilingual_357m).
- Existing speech manifest retains pinned MultiMed German source provenance.

Private results: campaign `cohorts/voice-coverage-r1`. Original errors stay in
place; output measurements and any fixes must distinguish historical releases
from subsequent retests. No quotas, model settings or runtime images are changed
by these helpers.
