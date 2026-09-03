# Model fast-start levels

Fast-start levels are a customer-facing performance contract for bringing a
cold model endpoint online. They deliberately hide the implementation choice:
customers select an understandable time class, while operators can inspect the
cache, snapshot, residency, placement, and benchmark evidence behind it.

The levels describe model startup after compatible GPU capacity is available.
They are not names for storage tiers and they do not include node provisioning
or queue wait.

| Level | Model-start target | Customer meaning |
| --- | ---: | --- |
| `Off` | No target | Use the conventional model-loading path. |
| `L1` | ≤ 300 seconds | Ready within five minutes. |
| `L2` | ≤ 120 seconds | Ready within two minutes. |
| `L3` | ≤ 60 seconds | Ready within one minute. |
| `L4` | ≤ 30 seconds | Ready within thirty seconds. |
| `Hot` | Already serving | A derived runtime state, never a configured level. |

The thresholds are qualification ceilings for an exact deployment tuple, not
a promise inferred from a selected cache mechanism. A conventional loader that
reliably meets 120 seconds may qualify for `L2`; an unproven GPU snapshot does
not qualify for any level merely because it is a snapshot.

## Clock and reported phases

One activation reports three values with explicit boundaries using monotonic
timestamps:

1. **Capacity wait** starts when the activation is accepted and ends when a
   compatible Ready node advertises enough allocatable GPUs and topology for
   the model Pod to be schedulable.
2. **Model start** starts at that capacity-available point and ends when the
   exact revision has passed its readiness probe and its Service has a Ready
   endpoint. This is the value evaluated against `L1` through `L4`.
3. **End to end** starts when activation is accepted and ends at the same
   endpoint-ready point. It is capacity wait plus model start.

Queue admission, preemptible-node acquisition, node boot, driver and device
plugin readiness, and topology scarcity belong to capacity wait. Image setup,
artifact restore, runtime initialization, weight transfer, compilation, and
readiness belong to model start. Time to first token or first generated media,
request latency, and steady-state throughput are reported separately; they do
not replace endpoint readiness.

If any boundary cannot be observed, the affected value is unavailable rather
than estimated. A model that already has a Ready endpoint is shown as `Hot`.
Its configured and qualified cold-start levels remain visible so an operator
can predict recovery after scale-to-zero or preemption.

## Desired, assigned, effective, and qualified state

The admin API and `ModelDeployment` status distinguish four values:

| Value | Meaning |
| --- | --- |
| Requested | The fixed level, or automatic minimum/maximum range, selected by the operator. |
| Assigned | The level the policy controller currently asks the runtime to provide. |
| Effective | The level supported by the mechanisms that are actually present and usable now; `Hot` is derived when an endpoint is Ready. |
| Qualified | The highest level backed by a current comparable benchmark cohort for the exact deployment tuple. |

An assigned level can be above the effective or qualified level while a cache
is being populated or a mechanism is unavailable, but the UI must show the
policy as unmet. It must never display the assigned value as achieved. Missing
evidence is `Unqualified`, not `Unsupported`: serving compatibility and
fast-start qualification are independent states.

The policy is optional so existing `ModelDeployment` revisions remain valid.
New revisions use one of these shapes:

```yaml
# Pin one customer performance target.
fastStart:
  mode: Fixed
  level: L2
  fallbackPolicy: AllowLowerLevel
```

```yaml
# Let the controller choose within explicit cost/performance bounds.
fastStart:
  mode: Automatic
  minimumLevel: L1
  maximumLevel: L4
  fallbackPolicy: AllowLowerLevel
```

`AllowLowerLevel` permits the controller to assign the highest qualified,
currently available lower level and reports the downgrade. `RequireTarget`
does not silently activate an unqualified lower acceleration path: the target
remains unmet until the exact path is available. Existing Ready replicas are
not destroyed merely because fast-start evidence becomes stale. Any separately
configured conventional availability fallback is reported as `Off`, not as a
successful target-level start.

## Automatic assignment

Automatic mode is bounded optimization, not an unrestricted heuristic. The
controller may consider only levels between `minimumLevel` and `maximumLevel`
that are qualified for the exact tuple and whose required mechanisms currently
report Ready.

For each model the controller reads payload-free one-hour and seven-day demand
aggregates from the platform PostgreSQL operation store. Those aggregates
contain request counts and idle-gap episodes based on the model's configured
idle timeout. Qualification p95 comes only from the separately retained
benchmark evidence; the legacy accepted-to-ready operation latency includes
capacity wait and is deliberately not reused as model-start evidence. Each
candidate level is scored using:

```text
expected hourly cost = mechanism resource cost
                     + cold activations per hour
                     × qualified p95 model-start seconds
                     × configured value of one wait-second
```

The cheapest eligible candidate is selected within the operator bounds. Idle
gaps determine activation episodes rather than treating every request as a
cold start. Capacity wait remains an independent signal for pool scaling and
does not cause the controller to misclassify a model loader.

The controller evaluates at most once every five minutes. Promotion requires
the next level to win three consecutive evaluations. Demotion is one level at
a time and requires the lower level to win for 24 hours. Promotion has a
30-minute cooldown; demotion has a 24-hour cooldown. The pure policy supports
target-miss acceleration, but the live controller leaves that input dormant
until the exact capacity-available-to-semantic-ready boundary is persisted in
the operation store; it does not relabel the existing accepted-to-ready value.
Mechanism loss changes `effectiveLevel` immediately, while assigned policy
changes still observe hysteresis.

If price data, latency value, sufficient history, qualification, or mechanism
health is missing, automatic mode fails closed to the configured minimum (or
the highest lower qualified level only when `AllowLowerLevel` permits it). It
does not invent a timing estimate. Keeping replicas hot remains an explicit
availability/autoscaling decision; `Hot` is reported from live endpoint state,
not selected as an automatic cache level.

Terraform supplies the economic inputs once for the cluster through
`deployment.dynamic_models.fast_start_wait_second_value` and
`deployment.dynamic_models.fast_start_mechanism_hourly_costs`. An omitted
mechanism cost currently means zero additional hourly cache cost; it never
creates a latency estimate or qualification. With one qualified mechanism the
controller can only choose levels supported by that path. Usage and cost change
the selected path once multiple qualified mechanisms are published for the
same exact deployment tuple.

Automatic assignment never rewrites an operator's artifact, runtime template,
cache tier, or snapshot identity behind their back. It compares only benchmark
paths compatible with that immutable desired tuple and publishes the selected
mechanism in status for the operator and mechanism adapters. Moving to a
physically different cache/snapshot/runtime path is a reviewed live `ModelDeployment`
revision when that path is already present in the infrastructure envelope; a
new storage or node-pool capability remains a Terraform change.

## Mechanisms are implementation details

The expected implementation progression is useful to operators, but it is not
a one-to-one level mapping:

| Common implementation path | Operator detail |
| --- | --- |
| Conventional | Pull the runtime and load immutable model artifacts normally. |
| Regional caches | Mirror OCI images near the cluster and retain immutable weights plus compatible JIT/compile caches. This is the usual first acceleration step. |
| Shared restore | Restore a runtime-native or GPU-process snapshot from enhanced object storage or a regional shared filesystem. |
| Node-local restore | Place a compatible snapshot on local NVMe and read shards in parallel. This needs a pool with local disks and a replacement strategy because the cache is ephemeral. |
| Host-memory residency | Keep compatible weights or process state in system RAM and transfer them into GPU memory on activation. RAM accounting and placement remain explicit. |
| GPU-resident cache | Keep a warm engine and its weights in GPU memory so activation is a promotion rather than a load. This holds an accelerator for as long as the replica is parked, so it depends on a hot floor that can afford it. |
| Optimized transfer/runtime | GDS, NIXL, NVIDIA Dynamo Snapshot, GMS, ModelExpress, or another qualified backend may shorten one or more phases. |

### Which mechanisms are implemented, and what each costs

Three are implemented as selectable adapters in
`components/control-plane/src/fs2_serve/fast_start_mechanisms.py`. A model pins
one with `spec.cache.mechanism`; leaving it unset keeps the historical
behaviour, where the fastest qualified path is selected from evidence.

| Mechanism | What it retains | What it costs |
| --- | --- | --- |
| `regional-cache` | In-region image mirror, retained payload, and the JIT/compile cache under an ABI-scoped sub-path instead of a discarded `emptyDir`; a bounded pre-read leaves the payload pages warm | Nothing reserved |
| `host-memory-residency` | A node-scoped holder keeps the exact payload in host RAM, or `runtime-sleep-offload` keeps a live engine's weights there | A scheduled host-memory reservation, requested and limited, capped at a quarter of the node |
| `gpu-resident` | A standby replica holds its warm engine in GPU memory behind a readiness gate | An accelerator, for as long as the replica is parked |

Each declaration's configuration is bound into the mechanism identity that
benchmark evidence must match, so retuning a mechanism starts a new cohort
instead of inheriting the previous one's percentile. Every mechanism is
projected into `status.fastStart.cacheMechanisms` with its price, and a
`Configured` mechanism sits next to whatever level the evidence supports, which
is `Off` until a cohort is populated.

`node-local-restore` and `shared-restore` are reported `Unavailable` for this
cluster's H100 pool, with the pool's own `local-nvme.fs2.nebius/eligible=false`
and `snapshot.fs2.nebius/eligible=false` selectors attached as the proof. They
are never attempted.

The runtime image, model artifacts, compiled kernels, snapshot, and host-memory
state are separate dependencies. A GPU snapshot does not by itself eliminate a
large image pull; the selected path must also make its exact runtime image
available quickly enough to meet the level.

ModelExpress moves compatible tensors from an existing donor to a new engine,
so its behavior depends on donor residency and exact runtime compatibility.
GMS decouples model state from a serving process and may eventually provide a
different transfer path. Both are mechanisms that can help a deployment meet
`L1` through `L4`; neither creates `L5` or `L6`. Adding future mechanisms does
not change the customer level scale.

## Qualification and evidence

A performance level is qualified only when one comparable cohort contains at
least 20 successful cold activations, its p95 model-start time is at or below
the level ceiling, every activation passes the semantic probe, and no eligible
failed or timed-out attempt has been discarded. A failure makes the default
cohort fail closed; it remains visible in the reliability result.

Three successful repetitions are enough for an **exploratory** result. The UI
may show its p50, p95, sample count, and timestamp, but must label it exploratory
and must not raise `qualifiedLevel` from it.

Comparable attempts bind at least:

- model identity and immutable revision;
- runtime image digest, launch configuration, and compile-cache identity;
- GPU model, GPU count, MIG shape, topology, node image, driver, and CUDA stack;
- region, pool, storage/cache tier, and immutable artifact or snapshot digest;
- the same controlled initial state and readiness/semantic probes.

Fresh image/artifact population and cache-hit restore are different cohorts.
Results do not transfer across a changed model revision, image, accelerator,
GPU count, driver/CUDA compatibility boundary, snapshot, cache tier, or startup
command. Such a change makes the old evidence historical and the live status
unqualified until re-tested.

Every retained receipt should contain the phase timestamps, raw attempt list,
success/failure result, p50/p95, sample count, exact tuple, mechanism inventory,
and benchmark-tool version. Capacity-wait and end-to-end percentiles are useful
operational evidence but do not decide the fast-start level.

### Exact runtime-evidence identity

New qualifying attempts use
`fs2-serve.nebius.ai/runtime-evidence-identity/v2`. The receipt, Terraform
envelope, controller and admin page carry the same canonical SHA-256 identity.
Compatibility is exact equality across these immutable groups:

| Group | Equality-bound values |
| --- | --- |
| Runtime | model/source revision, content and artifact-manifest digests, runtime profile and image digest, renderer-template digest, rendered command/arguments digest, non-secret environment digest, and their runtime-contract digest |
| Environment | reviewed project/region/cluster scope, accelerator class/product/capability/memory, driver/CUDA, host-runtime and storage-runtime component digests |
| Placement | accelerator count, topology policy and the post-capacity startup scenario |
| Cache | tier, mechanism configuration, snapshot and exact storage contract |
| Measurement | capacity-available-to-semantic-ready basis, request-payload digest, protocol/path/streaming, semantic validator, benchmark client and client placement |

The environment document explicitly lists each `(poolRef, capacityType)` member.
Regular and preemptible pools may share evidence only when one reviewed binding
names both members and all environment components are identical. A matching GPU
label alone never implies equivalence. An expired binding or any unavailable
expected component makes the current level `Off` while preserving the receipt
under `retainedPaths` with bounded mismatch paths.

Replica floors/ceilings, queue and access policy, OpenAI/MCP exposure, automatic
versus fixed selection, cost inputs and demand history are intentionally outside
the performance identity. Changing those policy fields does not invalidate a
still-identical executable runtime. Changing an artifact, render, environment,
placement, cache or measurement field does.

Legacy v1 receipts remain visible as `LegacyUnbound`; they cannot qualify a
level because their full current identity cannot be reconstructed safely. This
migration therefore leaves the retained Qwen and Cosmos n=3 campaigns at
`Off`. Removing the v2 environment/measurement file inputs is the rollback:
the controller retains all evidence but fails closed rather than falling back
to v1 compatibility rules.

## Current retained H100 boundary

The retained H100 deployment currently has a regional OCI mirror and shared
model cache. It does not have host-local NVMe, so the usual node-local snapshot
path is unavailable on that pool. No CUDA/GPU snapshot, host-RAM swap path,
GMS, or ModelExpress path is production-qualified there today.

Fresh exact-tuple campaigns on 2026-09-02 produced the following retained
exploratory results:

| Model and path | Successful attempts | Model-start p50 / p95 | Observed class | Qualified class |
| --- | ---: | ---: | --- | --- |
| Qwen3-8B, prepared reserved H100, process-cold shared-cache restart | 3/3 | 113.444 / 124.422 s | `L1` | `Off` |
| Cosmos 3 Nano, prepared preemptible H100, zero-Pod shared-cache start | 3/3 | 67.821 / 68.929 s | `L2` | `Off` |

Both tuples are complete and failure-free, but three attempts are not the
20 successful attempts required for qualification. The controller therefore
publishes their observed p50/p95 and `InsufficientBenchmarkSamples` while
leaving `qualifiedLevel`, `assignedLevel`, and the non-hot effective level at
`Off`. Qwen is independently shown as `Hot` while its minimum replica is Ready;
Cosmos returns to a zero floor.

The Cosmos samples include a strict native MP4/envelope semantic check. Their
queued request-to-valid-video p50/p95 was 82.771/84.561 seconds and generation
goodput p50/p95 was 1.672/1.688 frames/s. A separate same-backend warm call took
12.551 seconds for the checked-in 25-frame request, or 1.992 contract-derived
frames/s. Qwen's separate 90-request direct-runtime steady benchmark measured
62.937-62.940 output tokens/s, 13.742-15.545 ms median TTFT, and
15.685-16.907 ms p95 TTFT across three repetitions.

Negative evidence remains retained rather than pooled into a new campaign. A
fresh-node Cosmos attempt measured 158.041 seconds of capacity wait and
282.700 seconds of model start, including a 162-second 9.19 GB image pull, but
failed the then-current runtime-attribution collector before semantic output.
A later campaign exposed a Kubernetes Pod/EndpointSlice read-skew race. The
collector fixes started new compatibility tuples and campaign identities; they
did not rewrite, renumber, or discard those failures. Fresh-node, prepared-node,
and process-cold cohorts remain separate.

See [Live acceptance](LIVE_ACCEPTANCE.md) for the exact retained topology and
receipts, and [Dynamic model configuration](DYNAMIC_MODEL_CONFIGURATION.md) for
the runtime ownership and placement contract.
