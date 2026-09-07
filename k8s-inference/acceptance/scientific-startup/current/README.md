# Current-model startup campaign — 7 September 2026

[Current H100 measurements and interpretation](current-h100-20260907.md).

This campaign measures new starts, not historical complete-job durations.
Baseline solution source is `71547004`; the deployed control plane is
`adf1d842`. Scientific execution identities and general-serving image digests
are retained in each trial. Benchmark-only instrumentation must be identified
separately and must not change weights, precision, inference settings or output
validation. The production model catalog is not requalified by these probes.

## Clocks

Record observed boundaries, with their source and resolution:

1. Public request acceptance, when the public route is used. For an isolated
   probe use Kubernetes object creation instead; never label this a public
   request-to-ready measurement.
2. Scheduling/capacity acquisition, including new-node registration where it
   occurs. Existing capacity and newly provisioned preemptibles are separate
   cohorts.
3. Image pull, input/artifact preparation, and container process start.
4. Native model initialization/load completion, synchronized when necessary.
5. Application-ready for serving backends. Kubernetes PodReady is an additional
   observation, not a substitute for a model-ready marker.
6. First full valid result. For lazy/JIT scientific backends, explicitly record
   whether compilation happens after the chosen initialization marker; a loaded
   model is not automatically a warmed model.

Scientific batch workflows do not expose a persistent readiness endpoint.
Their per-stage initialization markers and first validated outputs must be
reported separately. In a multistage workflow, the first GPU stage becoming
ready does not mean all later stages are initialized.

Do not sum overlapping shard GPU-seconds into a wall-clock startup time. Do not
subtract inference from complete-job time to manufacture a missing boundary.
No missing metric is silently replaced with zero.

## Repetitions and caches

- At least three fresh-process observations per model for the principal
  measured condition, with fixed accepted input/settings, raw evidence, median
  and range. With three observations, do not claim meaningful p95/p99.
- Record image-present versus pulled, weight location, runtime compile-cache
  treatment, and host/filesystem cache state. Fresh process or fresh `emptyDir`
  does not establish disk-cold model weights.
- Qwen and Cosmos use isolated same-image serving probes; retained production
  Qwen remains available. Cosmos's first new-preemptible-node observation is a
  separate one-sample cohort, followed by three cached-image observations.
- Scientific workers first exercise unchanged public requests, then use
  minimal same-image instrumentation where upstream does not expose a reliable
  initialization marker. All normal scientific outputs are still validated.
- No host-wide cache flush, driver change, quota increase, GPU-mode change or
  model resource-limit increase. No B300 operation is part of this campaign.

Primary and structure lanes run independently, with bounded concurrency to
avoid exhausting host CPU/memory while many GPUs are free. General serving
uses at most two probe GPUs. Record shared-host conditions: these observations
are not dedicated-machine maximum-performance claims.

## Evidence capture

The parent captures private timestamped Pod and Event watch streams for the
model and academic namespaces. `capture_startup_logs.py` follows scientific
containers while their pods still exist and records an additional lifecycle
projection. Raw Pod specs/logs may contain workload handles and must not be
committed. Publish only redacted summaries and evidence digests.

For already-running private Pod watch files:

```bash
python3 capture_startup_logs.py \
  --watch /secure/campaign/models-pods.watch.json \
  --watch /secure/campaign/academic-pods.watch.json \
  --output /secure/campaign/container-capture \
  --kubeconfig /secure/cluster/kubeconfig --context inference-cluster
```

Stop the collector and watch processes after all assigned probes drain.
Temporary benchmark resources are removed by their owning worker; retain the
production cluster and original serving settings.
