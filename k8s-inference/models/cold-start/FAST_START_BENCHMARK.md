# Fast-start benchmark evidence

This benchmark contract measures the customer-facing model fast-start class
without presenting GPU capacity acquisition as model-loading performance. The
class clock starts when the exact requested GPU capacity is available and ends
when the model endpoint is ready. Capacity wait and activation-to-ready time
are retained as separate measurements.

| Class | GPU available to endpoint ready |
| --- | ---: |
| `Off` | No cold-start target |
| `L1` | at most 300 seconds |
| `L2` | at most 120 seconds |
| `L3` | at most 60 seconds |
| `L4` | at most 30 seconds |

`Hot` is an observed serving state, not a cold-start class, and therefore does
not appear in this receipt. A ready model can be hot while its configured cold
recovery path is still only qualified at L1.

## Evidence files

Legacy raw attempts use
`fs2-serve.nebius.ai/fast-start-benchmark-attempt/v1`. They remain valid
historical evidence but project as `LegacyUnbound` and cannot qualify a level.
New attempts use `fast-start-benchmark-attempt/v2` and add the complete
`runtime-evidence-identity/v2` plus its canonical digest. They record:

- the exact model content, runtime image, GPU, pool, cache/snapshot mechanism,
  storage, request, client, and source-code tuple;
- activation, capacity request and availability, endpoint readiness, request,
  first byte, first semantic output, completion, and return-to-floor clocks;
- capacity wait, GPU-available-to-ready, activation-to-ready, and inference
  durations;
- text or media work units, valid-output result, throughput, and hashes of the
  raw, semantic, runtime-log, and GPU-metric artifacts.

Unavailable environment facts are `null`; the collector must not infer a CUDA
version, driver, GPU product, compute capability, or storage identity. Such an
attempt remains useful exploratory evidence but cannot become qualification
evidence. Tokens, credentials, request bodies, model outputs, node IDs, Pod IDs,
and raw logs stay in the private run directory and never enter this receipt.

The aggregate schema is
[`fast-start-benchmark-receipt.schema.json`](fast-start-benchmark-receipt.schema.json).
[`aggregate_fast_start_benchmark.py`](aggregate_fast_start_benchmark.py)
validates every attempt, rejects mixed tuples or missing ordinals, calculates
nearest-rank p50/p95 and median absolute deviation, and derives the observed and
qualified levels. A receipt digest covers all evidence and derived values.

The live runner requires two reviewed, non-secret inputs before it changes any
replica or capacity state:

- `runtime-environment-qualification-set/v1` binds observed project, region,
  cluster context, accelerator, driver/CUDA, host runtime and storage runtime
  to explicit pool/capacity members and an expiry;
- `fast-start-measurement-contract/v1` binds the exact payload digest,
  protocol, endpoint, streaming behavior, semantic validator, benchmark
  client and client placement.

Both documents are self-digested. The runner verifies their raw values against
the live Pod, node, PVC, access bundle and request before emitting v2. It fails
before mutation when either input is structurally invalid. If an activation
fails before GPU/driver/storage facts can be observed, the failure remains a v1
`LegacyUnbound` attempt; v1 and v2 attempts cannot share a cohort.

## Clock interpretation

For an already-allocatable GPU, set `gpu_capacity_available` equal to
`activation_accepted`, omit `gpu_capacity_requested`, and record a zero
`capacity_wait`. For node scale-from-zero or preemption replacement, retain the
real capacity request and first allocatable-GPU timestamps. Never subtract an
estimated node startup duration.

The public gateway may accept and queue the same request that activates a cold
model. In that path `request_started` equals `activation_accepted` and can
precede `endpoint_ready`; the first semantic output must still follow endpoint
readiness. If a non-streaming client cannot observe the first response byte,
leave that timestamp and duration `null`. Label its first output as `response`;
do not call response-completion latency TTFT. A direct-runtime streaming sample
uses `first_output_kind=token`, `streaming=true`, and a different compatibility
tuple, so it cannot be mixed with public non-streaming repetitions.

For text generation, report output tokens per second. For image, video, audio,
embedding, or another native service, use the model's valid semantic unit such
as images, frames, generated seconds, items, requests, or bytes per second.
Keep request count, warmup, concurrency, payload digest, and output validation
fixed across repetitions.

## Aggregate and validate

Keep the run root mode `0700` and raw attempt and receipt files mode `0600`.
Aggregate one model, mechanism, capacity state, and request path at a time:

```bash
python3 k8s-inference/models/cold-start/aggregate_fast_start_benchmark.py \
  aggregate \
  --attempt-directory /absolute/private/run/qwen3-8b/prepared-node \
  --output /absolute/private/run/qwen3-8b/prepared-node-receipt.json

python3 k8s-inference/models/cold-start/aggregate_fast_start_benchmark.py \
  validate \
  --receipt /absolute/private/run/qwen3-8b/prepared-node-receipt.json
```

Three comparable successful attempts are the minimum useful exploratory run.
Their observed p95 and class may be shown, but `qualified_level` remains null.
Promotion requires at least 20 comparable successful attempts, no failed
attempts, a complete exact tuple, and a p95 that meets the class threshold.
Missing or failed runs must remain in the sequence; the contiguous ordinal and
unique raw-artifact requirements prevent selecting only the fastest attempts.

A representative live invocation includes both identity contracts:

```bash
python3 k8s-inference/models/cold-start/run_live_fast_start_benchmark.py \
  --kubeconfig /absolute/private/kubeconfig --context fs2-h100 \
  --access-bundle /absolute/private/access-bundle.json \
  --token-file /absolute/private/inference-token \
  --request-file /absolute/private/qwen-request.json \
  --environment-qualifications /absolute/reviewed/environment-qualifications.json \
  --measurement-contract /absolute/reviewed/qwen-measurement.json \
  --output /absolute/private/run/qwen3-8b/attempt-001.json \
  --raw-output /absolute/private/run/qwen3-8b/attempt-001.raw.json \
  --model-id qwen3-8b --deployment qwen3-8b \
  --service qwen3-8b --scaled-object qwen3-8b \
  --requested-level L2 --ordinal 1 --expected-floor 0 \
  --capacity-state fresh-node-zero-pod --mechanism shared-cache \
  --modality text --expected-text READY
```

## Publish evidence to the controller

The benchmark receipt remains the source of truth. Project one or more
validated receipts into the compact controller envelope, then reference that
machine-generated file from `deployment.dynamic_models.fast_start_evidence_file`:

```bash
python3 k8s-inference/models/cold-start/project_fast_start_evidence.py \
  --receipt /absolute/private/run/qwen3-8b/prepared-node-receipt.json \
  --receipt /absolute/private/run/cosmos3-nano/prepared-node-receipt.json \
  --output /absolute/private/run/fast-start-evidence.json
```

The v2 projection keeps every success and failure, the full compatibility tuple
and runtime-evidence identities, and expires after 30 days by default. The v1
projection explicitly sets `identityState=LegacyUnbound`. Terraform validates
the bounded wire shape and derives a runtime-only contract per model and pool
from the exact rendered command, arguments and non-secret environment. Set all
three reviewed inputs in `terraform.tfvars`:

```hcl
deployment = {
  dynamic_models = {
    fast_start_evidence_file                   = "/absolute/reviewed/fast-start-evidence.json"
    fast_start_environment_qualifications_file = "/absolute/reviewed/environment-qualifications.json"
    fast_start_measurement_contracts_file      = "/absolute/reviewed/measurement-contracts.json"
  }
}
```

The measurement set is
`{"schema":"fs2-serve.nebius.ai/fast-start-measurement-contract-set/v1","models":{...}}`.
When either current qualification input is omitted, Terraform invents no
binding: historical evidence stays visible and every L1-L4 result is `Off`.
The controller groups only exact compatible v2 identities, then applies the
20-success, zero-failure and p95 rules. Exploratory, LegacyUnbound, expired,
incomplete or mismatched evidence cannot raise `qualifiedLevel`.
