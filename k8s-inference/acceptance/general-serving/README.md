# General serving acceptance

`run_text_acceptance.py` repeats the existing Qwen exact-content fixtures over
the public API, validates output and runtime token counts, and preserves the
operation's timing and actual Pod/node/GPU identity:

```bash
uv run --project components/control-plane python acceptance/general-serving/run_text_acceptance.py \
  --bundle /private/access-bundle.json \
  --fixture catalog/runtime/validators/assets/qwen3-8b.json \
  --receipt /private/qwen-acceptance.json
```

The six 2026-09-06 H100 requests on source `29b7e01a` passed both exact oracles
in 0.410–0.600s accepted-to-completed. These are short, hot, nine-output-token
requests on the existing reserved GPU, not a maximum-throughput workload.
The receipt's output-tokens-per-second divides tokens by full operation time,
including admission; it is not isolated GPU decoding speed. The public gateway
does not stream, so TTFT is explicitly unavailable rather than equated with
completion latency. Private evidence is `qwen-public-29b7e01a.json` alongside
the retained Cosmos receipts below.

`run_cosmos_acceptance.py` exercises the native Cosmos3 Nano route using the
catalog's two pinned text-to-video fixtures, three repetitions each by default.
It validates the model/revision and media envelope, base64 decoding, MP4 container
signature, output sizes, and distinct outputs for the two different prompts.
This is the existing bounded media semantic contract, not a perceptual quality
assessment or a maximum-resolution stress test.

The runner uses the normal public HTTPS endpoint and reads credentials only from
the owner-only Terraform output bundle. It records operation IDs, input/output
digests, model/container identities, node snapshots, exact GPU attribution and
timings, but never the token, prompt text or generated media. It does not change
replica policies, node groups or quotas. Requests can naturally activate the
configured preemptible pool.

```bash
uv run --project components/control-plane python acceptance/general-serving/run_cosmos_acceptance.py \
  --solution "$PWD" --bundle /private/access-bundle.json \
  --kubeconfig /private/kubeconfig --context k8s-inference-h100 \
  --receipt /private/cosmos-acceptance.json
```

Use a fresh receipt path for every run. `--resume-from /private/failed.json`
recovers the first durable operation after a client interruption rather than
submitting that same workload again. The model and request identity must match.
The failed receipt remains intact, and recovered evidence is explicitly marked.
Polling retries transient transport failures and HTTP429/502/503/504 and records
them; authentication and other errors are not silently ignored.

## Timing interpretation

- `operation_end_to_end_seconds`: durable accepted-to-completed wall time. Use
  this for recovered operations as well as fresh ones.
- `operation.cold_start_seconds`: the gateway's accepted-to-runtime-ready clock;
  it can include capacity acquisition, queueing and retries. It is **not** the
  capacity-available-to-ready clock that qualifies a fast-start level.
- `end_to_end_seconds`: how long this client invocation observed the case,
  including polling. A recovered completed operation can take less than one
  second to fetch without having cold-started that quickly.

The before/after snapshots and operation Pod/node/GPU identities distinguish
hot requests, replica cold starts and preemptible capacity acquisition. Compare
only matched cohorts; a deployment rollout/retry is not a clean cold-start
baseline. Keep warmup/activation cases separate from warm latency summaries.

## Retained 2026-09-06 observations

All six bounded Cosmos outputs passed on preemptible 1× H100. The initial activation
survived a control-plane rollout and worker retry: 468.16s accepted-to-completed,
including 467.01s accepted-to-ready; the initial client polling failure is retained
separately. The next replica cold activation on existing capacity took69.13s
accepted-to-completed (67.72s accepted-to-ready). Four subsequent hot requests
took 1.25–1.55s in the durable operation clock; the 3s polling interval makes client
observations 4.18–4.36s. These small samples are not p95/p99 claims.

Private evidence: `/home/tux/.local/state/fs2-readiness-review-r20260906/cosmos-recovered-acceptance.json`.
The original failed client receipt is `cosmos-acceptance.json` in that directory.
Neither result claims that Cosmos GPU checkpointing is deployed.

The later [8bb53aab public canaries](evidence/h100-8bb53aab-canary-20260906.md)
passed two Qwen and two Cosmos requests on the current deployed path without
changing floors or capacity. That report separates the observed replica-cold
activation from the warm request and pins fixture, output, and receipt hashes.
