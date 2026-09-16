# MindGuard safety classifiers and private clinician intake

`mindguard-4b` and `mindguard-8b` are English mental-health **safety classifiers**.
They are separate from the private MindGuard v2 clinician. Results are observations;
they never block, censor, rewrite or stop a conversation. Their checkpoint chat
templates evaluate **only the last user message**, using preceding context. A
complete transcript needs one prefix evaluation per user turn.

The public model revisions, source dataset revision and existing Scientific AI
runtime image are pinned in `public-models.lock.json`. The files were accessed with
the configured authorized Hugging Face identity. Tokens and weights are not stored
in Git. The public checkpoints contain float32 weights; this candidate serves them
as BF16 with vLLM, a 4096-token context limit from the model-card usage example,
temperature zero, seed zero and at most 15 generated tokens. The delivered chat
template is explicitly loaded from the same pinned snapshot. Oversized inputs fail
visibly; they are never silently truncated. A longer classifier context requires a
separate measured profile.

## Integration contract

Import `MindGuardMessage`, `assess_mindguard`, and
`assess_mindguard_transcript` from `fs2_serve.mindguard`. The async helpers use the
caller-owned `httpx.AsyncClient` and an operator-configured runtime base URL ending
in `/v1`; an absent endpoint returns `unavailable`. Model IDs are restricted to the
two public classifiers. Caller code must persist the returned typed results and
errors alongside its authenticated run, with ordinary tenant and admin visibility.
The helpers do not create a second authorization or storage path.

Each assessment contains model identity/revision, observational role, status,
safety label, category codes (`S1`: self-harm risk; `S2`: threats to others including
abuse/neglect), latency, token usage and exact context/target coverage. Failed,
malformed, incomplete or wrong-model responses have no safety label. The transcript
result retains every assessment and reports evaluated/total user turns and flagged
message indices. It does not reinterpret the final user turn as whole-conversation
safety, judge clinician quality or produce a clinical diagnosis.

## Reproduce the preview and measurements

Create a virtual environment and install `requirements.lock` with hash checking.
The runtime itself is the immutable OCI image in the model lock; the environment
here is only for measurement and handoff validation.

```bash
uv venv .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
python render_preview.py mindguard-4b --node EXISTING_FREE_L40S_NODE
```

The renderer emits a task-owned PVC, Deployment and internal Service in
`fs2-models`. Its init container expects Secret `fs2-mindguard-r20260916-hf`, key
`token`, containing an already authorized HF credential. The credential is mounted
only into the downloader environment; the serving container is offline and mounts
the exact model snapshot read-only. Never include credential values in commands,
reports or Git. Validate the rendered manifest with `kubectl apply --dry-run=client`
using the explicit intended kubeconfig/context before applying. GPU topology and
node are operator choices; the current renderer reserves one existing regular
L40S because the preemptible H100 nodes were unavailable during this run. It does
not create capacity, change quotas or alter shared services.

Pin both comparison runs to the same node and use one GPU at a time. Record the
first startup, repeat with warm image/weights, then measure each checkpoint:

```bash
python capture_runtime_evidence.py \
  --kubeconfig /SECURE/PATH/kubeconfig --context TARGET_CONTEXT \
  --model mindguard-4b --cache-state cold-image-cold-weights \
  --output /SECURE/EVIDENCE/4b-runtime.json
.venv/bin/python benchmark.py --model mindguard-4b \
  --endpoint http://127.0.0.1:18144/v1 --source representative --repeat 3 \
  --hardware-evidence /SECURE/EVIDENCE/4b-runtime.json \
  --output /SECURE/EVIDENCE/4b-representative.json
.venv/bin/python benchmark.py --model mindguard-4b \
  --endpoint http://127.0.0.1:18144/v1 --source model-card --repeat 10 \
  --output /SECURE/EVIDENCE/4b-model-card.json
.venv/bin/python benchmark.py --model mindguard-4b \
  --endpoint http://127.0.0.1:18144/v1 --source testset --concurrency 4 \
  --output /SECURE/EVIDENCE/4b-sword-testset.json
```

Use the corresponding model ID and local port for 8B. The exact model-card example
is extracted from its pinned source at runtime. The testset adapter loads all 1134
rows from pinned Parquet, appends each annotated `user_message` to its `prompt`
history, and maps the dataset's `safe`, `self_harm` and `harm_others` labels.
`--source mindeval --input /PRIVATE/transcripts.jsonl` accepts completed transcripts
and expands all user-turn prefixes. Each JSONL row has `id` and `messages`; optional
`expected_by_user_index` maps an input message index to `safety` and `categories`.
Without supplied labels this is a runtime/coverage test, not an accuracy estimate.

The report separates first-request, warmup and measured requests, excludes warmup
from percentiles/throughput, counts errors explicitly, and records per-case input
hashes without transcript text. A measured run needs at least ten requests.
Throughput uses the whole concurrent measurement wall time. Accuracy, category
agreement, false positives and false negatives are calculated only for completed
labeled cases; completion rate is always shown. AUROC remains null because discrete
labels do not establish a calibrated risk score. Representative probe results must
not be presented as clinical validation or replace the Sword testset.

Live evidence lives in `evidence/`. The source testset is not redistributed. Runtime
evidence includes pod/node/GPU identity, immutable image, software versions, observed
GPU allocation/utilization and startup clocks, with cache state kept separate.

## Private MindGuard v2 intake

No private v2 bundle has been supplied. `MindGuardV2Bundle` in
`fs2_serve.mindguard_artifacts` requires the delivered source location/revision,
approval-record digest, config, tokenizer/config, chat template, all safetensors
shards/index and known-good serving configuration. File size and SHA-256 checks
cover the entire manifest; paths cannot escape the artifact root. The supplied
config must support 32768 tokens and serving uses an immutable image with BF16.
Public Qwen base checkpoints and public MindGuard classifiers are rejected as v2
substitutes. Source references must not embed credentials or signed URLs.

```bash
.venv/bin/python validate_private_bundle.py \
  /PRIVATE/approved-manifest.json /PRIVATE/delivered-bundle
```

This verifies supplied artifact integrity only. It never claims GPU readiness or
MindEval acceptance. `MindGuardV2EventBinding` binds an approved manifest digest to
an internal `mindguard-v2-event-EVENT_ID` service, ordinary gateway admission, BF16,
32k context and at least one hot replica. Production endpoints, production fallback,
zero hot replicas and unqualified FP8 are rejected. Do not deploy until the exact
bundle is supplied and its known-good runtime is reproduced. Deterministic prompts,
32k-context behavior, multi-turn MindEval, load, recovery and rollback still need
measurement. FP8 needs a separate BF16 parity qualification before any activation.

Source references: [MindGuard-4B](https://huggingface.co/swordhealth/MindGuard-4B),
[MindGuard-8B](https://huggingface.co/swordhealth/MindGuard-8B),
[MindGuard-testset](https://huggingface.co/datasets/swordhealth/MindGuard-testset),
[MindEval](https://github.com/SWORDHealth/mind-eval).
