# Cold-start mechanism campaign

This directory measures each cold-start mechanism against the conventional
loader on live H100, so every model can be given more than one startup path and
tuned against real workloads later.

The production mechanisms and experimental GPU-resident adapter are **not** here. They live in
`components/control-plane/src/fs2_serve/fast_start_mechanisms.py` and are
rendered by the model controller or this campaign harness. Production
`gpu-resident` selection stays disabled until a controller owns its promotion
readiness condition. This directory only sequences attempts, times them, and
writes receipts.

## Why this is Python and not a chart, a template, or model onboarding

A fair question, since the platform leans on Helm. The answer differs per piece,
and in every case the rule was to extend what exists rather than add a parallel
system.

| Piece | Where it lives | Why |
| --- | --- | --- |
| The mechanisms | Control-plane renderer | Model workloads are rendered by the model controller from the infrastructure envelope at runtime, not by Helm. Helm owns the control plane itself. Putting mechanism rendering in a chart would mean two renderers disagreeing about one Pod. |
| The CRD change | `charts/control-plane/.../crds` | Helm already owns the CRD, so the new `spec.cache.mechanism` and `status.fastStart.cacheMechanisms` went there. |
| Per-model configuration | One reviewed JSON document read by Terraform | This reuses the existing `fast_start_evidence_file` / `fast_start_environment_qualifications_file` / `fast_start_measurement_contracts_file` pattern. Onboarding the two hundredth model is another entry in that document, not new Terraform and not new code. |
| The residency agent | Inside the control-plane package | Shipping it with the code means onboarding a model needs a declaration and nothing else. It is not a chart asset because no chart renders the holder; the controller does. |
| This campaign | Python here | There is no existing harness that can cycle a task-owned Pod per mechanism arm. Crucially, it renders its arms by calling the same adapter functions as the production renderer (including the explicitly experimental GPU arm), so a measured improvement is attributable to the shipped implementation rather than to a duplicate benchmark fixture. A chart would have to restate the Pod shape and would then be measuring itself. |

`model-onboarding/compile_model.py` is the natural place to *generate* the
reviewed mechanism document once a model's declaration is settled; it is the
existing compiler for per-model declarations. That is a follow-up, not a
different design: the document shape it would emit is the one Terraform already
reads here.

## What the campaign measures

One attempt is timed from compatible accelerator capacity being available to the
first validated semantic response, which is the basis `fast_start.py` uses.

* **Cold arms** are timed from the Pod's `PodScheduled` condition to the first
  validated response, both cluster-side clocks. The condition timestamp has
  one-second granularity and every receipt records that.
* **Promotion arms** activate an already-parked replica. They are timed inside
  the cluster from the prober's own clock, and the trigger is dispatched after
  the prober starts waiting, so the dispatch counts against the mechanism and
  the reported activation is conservative. Their receipts set
  `capacity_preheld` and record the accelerator or host RAM the parked replica
  holds, because that is the price of the faster activation.
* **Warm-ups are excluded.** A retained compile cache has to be written once
  before it can be read; measuring that first write as the steady state would
  understate the mechanism. Warm-up receipts are kept, named `warmup-*`, and
  never enter a cohort.
* **Failures are kept.** A failed or timed-out attempt is recorded with a null
  duration, because a startup class is a reliability claim as well as a
  latency percentile.

Page-cache state on the shared filesystem is not controlled between cold
attempts and every receipt says so. `host-memory-residency` is the only arm that
holds the payload deliberately, and the per-attempt runtime weight-load seconds
make the difference visible either way.

## Nothing here qualifies a level

The runner reports whether a cohort reaches 20 comparable failure-free samples
and what its p95 is. It never grants a level, and every summary row carries
`qualifies_a_level: false` with the owner named. Only
`fs2_serve.fast_start.evaluate_fast_start` decides a level, from evidence bound
to the exact deployment tuple.

## Running it

```bash
cd k8s-inference/models/fast-start-mechanisms

python3 mechanism_arms.py --arm regional-cache        # inspect a rendered arm

python3 run_mechanism_campaign.py \
  --kubeconfig "$KUBECONFIG" --context k8s-inference-h100 \
  --node "$H100_NODE" --campaign-id q1 \
  --arms conventional regional-cache host-memory-residency \
         host-memory-residency-sleep-offload gpu-resident \
  --samples 20 --control-samples 20 \
  --output-dir "$RUN_ROOT/qwen3-8b"

python3 run_mechanism_campaign.py ... --teardown      # remove everything
```

Raw per-attempt receipts name the node they ran on, which the public export
rule forbids in the checked-in tree, so they stay in the run root. Only the
redacted comparison and `EVIDENCE.md` are committed.

## BoltzGen

`campaign-contract.json` already carries a BoltzGen target, marked
`pending-serving-runtime`. BoltzGen has a qualified H100 runtime image but no
readiness-probed serving endpoint on this cluster yet; its scientific batch
adapter runs as a Job. The moment it serves, filling in the same keys as the
Qwen target and running with `--target boltzgen` measures it. No mechanism code
changes are needed, which is the point of keeping the arms declaration-driven.

Qwen is the proving ground only. It is the least important model on this
cluster, and the campaign never touches the serving Qwen deployment: every arm
is a separate task-owned Pod with its own Service.
