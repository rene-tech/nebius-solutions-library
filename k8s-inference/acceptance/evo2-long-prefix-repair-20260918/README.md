# Evo2-40B long-prefix memory repair

Status: v1 and v2 passed their complete isolated numerical and 96-request suites.
V2 additionally passed production-timing health and concurrent/duplicate checks.
The candidate promotion package is prepared, not applied by this lane;
customer-path qualification remains the release owner's next gate.

## Observed failure and fixed contract

The scientific qualification campaign's public Arabidopsis chloroplast corpus
contains four positions, four prefix/output shapes and two seeds: 32 requests.
All eight 8192-base-prefix/512-generated-base cases failed; the 24 smaller cases
completed. Retained logs tie sampled failures to an 8 GiB modal-state FFT
allocation when approximately 6.49 GiB remained on the first H100. This is a
runtime workspace defect, not an external capacity shortage. The old HTTP
handler let the exception close the socket; the relay then returned retryable
503, obscuring the cause and repeating the same failed operation.

The repair tiles independent hidden channels in Vortex's modal FFT prefill.
It retains the same FFT length, FP32 exponentials, complex64 transforms, final
state dtype, sequence order, checkpoint, native TE FP8/BF16 projections,
HCS/HCM/HCL kernels and per-layer CUDA device guard. It neither reduces nor
raises accepted prefix/output lengths and does not change the scientific
generation parameters. It is not sequence chunking or quantization.

Only a recognized `MODEL_MEMORY_EXHAUSTED` error from Evo2, with valid bounded
shape counts and `retryable:false`, becomes a finite
`model_memory_exhausted` operation. Loading, preemption, network failures and
unrecognized 500 responses keep their existing retry behavior. A failed
request releases its temporary allocations without unloading model weights.

The v2 server handles health GETs concurrently while serializing the complete
POST admission/replay/generation path under one lock. Exactly one GPU generation
can run, and duplicate request IDs remain atomic. Production probe periods and
timeouts are unchanged. `observe_health.py` uses the actual three-second
deadline during all 96 calls; `concurrency_http.py` then checks two real requests,
a duplicate ID and responsive health, with server-side intervals retained.

## Pinned environment

- Current baseline image: `evo2-runtime@sha256:383f9979021bd3fe018c4dbba675610e0a5f7282b2164db1d8386611351de6f5`.
- Historical v1: `evo2-runtime@sha256:38370c547d9000684a2a46986562d275024877f3016bfadd7805d68f384ed850`.
- Candidate v2: `evo2-runtime@sha256:8c1a5dca0c32b497e04afaf93215809499a5736666540f4eb5b344dd84b709ee`.
- Regional image prefix: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/`.
- Model revision: `d529aa57c30771814217ad89baaeaf6e2315c7d7`.
- Checkpoint SHA-256: `dd299612b1c1cdded0dfdcaf4d16f98fc97458261d80f4d662429f0ccb316bc3` (82,253,491,694 bytes).
- Pinned installed Vortex `engine.py` SHA-256: `9e347d056f04a9ab151770eaafe8ad65a4600980a356440ee197ee2f220127df`.
- Pinned installed `generation.py` SHA-256: `d39c1bb66b036f7e61c0ff4f44390c1b655661fc8cf7f9f4d8a4c7bceb4a1e4f`.
- Target: two H100 80 GB HBM3, SM90, same-node layer model parallelism.
- Project `project-e00rene`, eu-north1, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, namespace `fs2-models`.

The isolated candidate mounts the existing model PVC read-only. Compilation
caches and evidence are separate temporary writable volumes. No production
Service selects its labels. Available single preemptible GPUs can run the
state-only proof; full-model proof needs two collocated GPUs. Waiting for that
placement is recorded separately, without changing quotas or disrupting jobs.

## Reproducible acceptance

1. `models/general-media/tests/test_evo2_prefill.py` compares the literal pinned
   upstream operations against the tiled function over twelve CPU shapes,
   including grouped filters, two batches, uneven tile boundaries and varied
   lengths. All full state values must match, not just aggregate norms.
2. `qualify_runtime.py --state-only` compares the actual installed upstream
   function with the candidate on CUDA, recording full-state error statistics
   and peak memory. Three H100 shapes passed bit-for-bit. At 1024 channels,
   4096 prefix positions and 16 modal states, peak allocated memory was
   3,565,256,704 bytes originally and 948,160,512 bytes tiled. This isolated
   measurement is **not** a model-level latency or capacity claim.
3. Without `--state-only`, the harness loads the exact checkpoint on two H100s.
   It then compares original A, original B and tiled execution over three
   prefix lengths and three seeds, retaining all last-position logits at each
   generated token and complete responses. Baseline repeat drift is reported
   separately. The gate requires identical seeded outputs and all logits
   within `rtol=1e-4, atol=1e-4`. This instrumented eight-output-token sub-study
   is explicitly separate from the original 32-request campaign.
4. Once those checks pass, the same candidate serves HTTP. `replay_http.py`
   submits **all 32 original requests, three repetitions**, starting with the
   formerly failing shape. There are no automatic retries. Every response,
   error, timing and reference metric is retained. The server records request
   shape hashes and GPU peak allocation for each call. Run against an internal
   test connection, not a public production route.
5. Parent integration owns exact-image promotion, ordinary authenticated MCP
   regression, sibling-feature checks and customer-facing release decision.
   Isolated model HTTP success alone is not customer acceptance.

The public sequence reference is NCBI `NC_000932.1`, from the Arabidopsis
chloroplast publication [Sato et al.](https://pubmed.ncbi.nlm.nih.gov/10574454/).
Training overlap is unknown. GC fraction and positional reference identity are
descriptive generation metrics, not biological fitness, variant-effect
accuracy, experimental validity or reproduction of that publication.

## Current evidence and commands

Protected raw evidence root:
`/home/tux/secure-handoff/evo2-long-prefix-20260918-WfESUj`.
Original corpus:
`/home/tux/secure-handoff/librechat-rene-20260918/qualification-general-v1/cases.json`.
Original customer-operation receipts:
`/home/tux/secure-handoff/scientific-qualification-20260918/cohorts/general-r1`.
Sampled original exception logs:
`/home/tux/secure-handoff/scientific-qualification-20260918/general-r1-diagnosis/evo2-19h34-logs.json`.

CPU checks from the solution root:

```sh
python3 models/general-media/tests/test_evo2_h100.py
python3 models/general-media/tests/test_evo2_errors.py
python3 models/general-media/tests/test_evo2_prefill.py # requires PyTorch
```

Control-plane checks from `components/control-plane`:

```sh
uv run --frozen pytest -q tests/test_runtime_scientific_errors.py tests/test_admission_workers.py tests/test_runtime_and_schema.py
uv run --frozen ruff check src/fs2_serve/runtime.py src/fs2_serve/admission.py tests/test_runtime_scientific_errors.py
```

At 20:57 UTC, 133 control-plane tests and 15 runtime test methods passed,
including twelve CPU state-comparison fixtures. The CPU original-function
reference emits PyTorch's documented complex-to-real cast warning; this is the
same explicit upstream final-state projection, not an unexamined runtime error.

V1 completed 96/96 calls: all 24 formerly failing long-shape repetitions passed
in 23.98–27.09 seconds. All 72 comparisons with originally completed shorter
requests produced identical sequences. Nine paired full-model comparisons had
bitwise-identical every-token logits, and six CUDA state checks matched exactly.
The retained report is `v1-final-report.json`; its customer-path verdict is false.

V2 also passed the full 8192-channel × 8192-prefix × 16-state CUDA comparison on
both H100s. The isolated state workspace peak dropped from 57,043,124,224 to
2,954,446,848 bytes, bitwise unchanged; this is not total model memory. Its
96-request replay completed successfully, with 260/260 health probes meeting the
three-second deadline (maximum 0.3163 seconds). The subsequent concurrent long
and short requests both passed; the duplicate returned 409 without executing.
Exactly 98 server receipts prove one active GPU generation and no overlap.

V2 median end-to-end internal HTTP latency was 3.129 seconds (256+64), 6.054
seconds (1024+128), 12.320 seconds (4096+256), and 24.431 seconds (8192+512).
The long-shape maximum was 27.226 seconds, including its first-use overhead;
maximum allocated memory was 56.994/56.850 GB on the two GPUs. All 72 comparisons
with previously completed shorter customer requests have identical sequences;
all nine paired full-model comparisons and eight CUDA state comparisons are
bitwise equal. These timings exclude public queueing and are not cold starts.

Full logs, identity, source-byte hashes and numerical receipts are retained.
The isolated v2 Pod and both task ConfigMaps were deleted after capture; the
temporary internal port-forward was stopped. Two H100s were released. The
combined CT/Evo2 candidate, rollback values/specs and validators are under
`ct-evo2-candidate/` in the protected evidence root. Recheck its release-159
baseline before applying over a newer control-plane release.

Known inherited warning: Transformer Engine 2.8 reports its supported
FlashAttention range through 2.8.1, while the pinned base image contains
2.8.3.post1. Installed Vortex uses TE Linear/FP8 recipes and its own
`vortex.ops.local_flash_attn_*` implementation, not TE DotProductAttention,
whose import emits this warning. The candidate changes neither package.
This compatibility debt remains documented; numerical tests found no drift.
