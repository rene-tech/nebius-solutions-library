# Qwen3-8B cold-start mechanism comparison

One campaign, five arms, 20 measured attempts each, zero failures. Qwen3-8B is
the proving ground only; it is the least important model on this cluster and the
serving deployment was never touched.

## Where and how

Project `project-e00rene`, region `eu-north1`, cluster context
`k8s-inference-h100`, namespace `fs2-models`, pool `h100-reserved-8x`, one H100
SXM5 80 GB capacity-block node whose eight GPUs were otherwise idle. Every arm
ran on that same node, as a separate task-owned Pod and Service, using the same
runtime image digest, the same immutable payload
(`sha256:5b0f0f64…8971d`, 16,397,461,266 bytes) and the same model arguments.
Only the mechanism differed, and each arm was rendered by calling the production
adapter functions rather than a benchmark fixture.

Attempts alternated strictly between the control arm and each candidate, so
drift on the node or the shared filesystem could not favour one arm. One warm-up
per cold arm populated its retained state and is excluded.

| Mechanism | n | fail | p50 s | p95 s | vs conventional | Reserved while idle |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `conventional` | 20 | 0 | 101.64 | 106.54 | control | nothing |
| `gpu-resident` | 20 | 0 | 1.60 | 1.83 | 104.7 s faster, 58.1x | 1 accelerator |
| `host-memory-residency` | 20 | 0 | 106.49 | 106.68 | 0.1 s slower | 18.0 GiB host RAM |
| `host-memory-residency-sleep-offload` | 20 | 0 | 0.56 | 0.71 | 105.8 s faster, 149.8x | 1 accelerator, 18.0 GiB host RAM |
| `regional-cache` | 20 | 0 | 61.53 | 61.66 | 44.9 s faster, 1.7x | nothing |

Runtime phases, median over each cohort's successful attempts:

| Mechanism | weight load | torch.compile | graph capture | engine init |
| --- | ---: | ---: | ---: | ---: |
| `conventional` | 2.90 | 28.12 | 5.50 | 40.32 |
| `gpu-resident` | n/a | n/a | n/a | n/a |
| `host-memory-residency` | 2.93 | 27.98 | 6.00 | 39.97 |
| `host-memory-residency-sleep-offload` | n/a | n/a | n/a | n/a |
| `regional-cache` | 3.02 | 0.38 | 5.00 | 10.32 |

- `conventional`: 20 samples, 0 failed, reaches the 20-sample failure-free rule.
- `gpu-resident`: 20 samples, 0 failed, reaches the 20-sample failure-free rule.
- `host-memory-residency`: 20 samples, 0 failed, reaches the 20-sample failure-free rule.
- `host-memory-residency-sleep-offload`: 20 samples, 0 failed, reaches the 20-sample failure-free rule.
- `regional-cache`: 20 samples, 0 failed, reaches the 20-sample failure-free rule.

Reaching the rule is necessary, not sufficient, and this script never grants a level. fs2_serve.fast_start.evaluate_fast_start decides one, from evidence bound to the exact deployment tuple.

## What the numbers say

**`regional-cache` is the cheapest real win and it is large.** It reserves
nothing and removes 44.9 s from p95. The phase table shows exactly where:
`torch.compile` falls from 28.12 s to 0.38 s, and engine initialisation from
40.32 s to 10.32 s, because the live render discards its JIT cache with the Pod
and this mechanism retains it under an ABI-scoped sub-path. Its p95 of 61.66 s
misses the L3 ceiling of 60 s by 1.66 s, so the next worthwhile piece of work is
the 5 s of CUDA graph capture.

**`host-memory-residency` is insurance, not speed, on an idle node.** It is
0.1 s slower than conventional, and the phase table explains why honestly:
weight load is 2.93 s against conventional's 2.90 s. On a node with 1.5 TiB of
RAM that re-reads the same 16 GiB every couple of minutes, the page cache is
already warm, so the guarantee buys nothing and the residency admission
handshake costs a little. Its value is on a busy node where the cache would be
evicted, and that is not what this node was. The mechanism is worth keeping and
worth measuring again under memory pressure; on this evidence it should not be
selected here.

**Keeping the engine alive is what transforms the number.** Offloading the
weights to host RAM with the engine asleep gives a p95 of 0.71 s, and parking a
warm engine in GPU memory behind a readiness gate gives 1.83 s, against 106.54 s
conventional. Both hold an accelerator the whole time they wait, which is the
price and is recorded in every receipt. These are activations of an already
parked replica, timed conservatively from inside the cluster with the trigger
dispatch counted against the mechanism.

## What this evidence does not do

It does not qualify a level for anything. These cohorts belong to a task-owned
tuple: this campaign's own Pod, Service and compile-cache claim. The production
`qwen3-8b` ModelDeployment is a different exact tuple, and
`fs2_serve.fast_start.evaluate_fast_start` admits evidence only for the tuple a
revision will actually run. Turning these into a qualified level means running
the same campaign against the production tuple and projecting the receipts into
the infrastructure envelope, which is a separate, reviewed step.

Nor is the residency mode the strongest one available in principle. Guaranteed
page locking is impossible on this cluster for a non-root runtime identity:
measured as uid 1000 with `capabilities.add=[IPC_LOCK]`, `CapBnd=0x4000`,
`CapEff=0x0` and `RLIMIT_MEMLOCK=8388608`. Added capabilities reach only the
bounding set for a non-root container, so `mlock` returns ENOMEM. The holder
runs `mapped-payload-residency` and reports `residency_guaranteed=false`.

Page-cache state between cold attempts was not controlled and every receipt
records that. The `PodScheduled` condition that starts the cold clock has
one-second granularity, which is under 2% of a 106 s measurement and is recorded
per receipt.

## Reproducing it

Raw per-attempt receipts name the node they ran on, which the public export rule
forbids in the checked-in tree, so they stay in the run root. `comparison.json`
here is the campaign's own aggregate. Regenerate the tables above with:

```bash
python3 models/fast-start-mechanisms/format_comparison.py --receipts "$RUN_ROOT/qwen3-8b"
```
