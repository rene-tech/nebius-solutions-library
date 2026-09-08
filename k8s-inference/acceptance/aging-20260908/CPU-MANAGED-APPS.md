# CPU-managed Apps integration

Implementation checkpoint: 2026-09-08 13:20 UTC, with the explicit scale-to-zero onboarding correction below added afterward. Source is prepared for the root-coordinated release; this is **not** a claim that a public CPU App has completed live scale-from-zero acceptance.

## Contract

CPU Apps reuse the existing ModelDeployment controller, KEDA operation-demand and startup-retention triggers, native route publication, durable operation history, owner attribution, and revision fences. There is no second scheduler or fake GPU allocation.

```yaml
placement:
  poolRefs: [batch-cpu]
  acceleratorsPerReplica: 0
  topologyPolicy: SingleNode
  cpuResources:
    cpuMillis: 1000
    memoryBytes: 536870912
queue:
  localQueue: general-cpu
```

The CPU/RAM numbers above illustrate the contract, not an automatic resource recommendation. The installed runtime qualification must carry the exact same `cpuResources`, `maxAcceleratorsPerReplica: 0`, `acceleratorClasses: [CPU]`, and `localQueue: general-cpu`. The renderer checks the scheduler-effective **whole Pod**, including regular containers, native sidecars, init-container maxima, resource-limit fallback, and Pod overhead. A mismatch is rejected before resource rewriting.

The infrastructure pool declares `resourceName: cpu`, `acceleratorsPerNode: 0`, `allocatableCpuMillis`, and `allocatableMemoryBytes`. Its exact identity selector is `capacity.fs2.nebius/pool-id`. CPU and RAM must both fit:

```text
replicas per node = min(floor(allocatable CPU / full-Pod CPU),
                       floor(allocatable RAM / full-Pod RAM))
configured replica ceiling = replicas per node × configured max nodes
```

This is configured infrastructure headroom, not instantaneous free capacity or a provider availability guarantee. The existing `batch-cpu` contract supplies 7000m CPU and 28672 MiB RAM per node, min 1/max 2, and the `general-cpu` LocalQueue. No node or quota change is part of this extension. Terraform capacity suggestions point to the owning `cpu_pools.<id>.max_nodes` input, not the derived workloads envelope.

CPU image-owned artifacts use the existing `Disabled` external cache tier. Their artifacts may be a reference database, baked weights, or an explicitly identified formula. CPU qualification does not expose GPU snapshot, residency, ModelExpress, or GPU-bound fast-start evidence. Clinical PhenoAge remains a CPU formula; AltumAge's separately qualified GPU runtime is not evidence of snapshot acceleration.

The additional CPU fields are omitted when unset. Old GPU resource defaults, nested serialization, spec digests, and infrastructure-envelope digests remain unchanged. The native runtime model schema is derived from a fresh copy of the archived schema; archived catalog qualification closures are not rewritten.

## Avoiding a qualification deadlock

The old controller rejected an enabled zero-replica floor unless `scaleToZeroQualified` was already true. That prevented a new App from obtaining its first real scale-from-zero measurement. Earlier models qualified through a separate Terraform-owned static deployment before controller adoption; repeating that ownership detour for each new App was unnecessary.

An explicitly selected zero floor is now accepted with the existing validation warning `scale_to_zero_unqualified`. Its qualification flag remains false. Unmeasured configuration defaults remain at **one hot replica**, and configuration metadata exposes `scale_to_zero_warning`; neither a runtime readiness receipt nor this configuration change counts as elasticity evidence.

The normal operator can therefore create an independent acceptance App, explicitly choose zero, and observe real durable demand → KEDA/HPA → Ready → two useful semantic results → natural zero cleanup. Only that completed receipt promotes the measured qualification. Customer source Apps need not change during the test. There is no new override, permission, or exception mode. Artifact, runtime, pool, snapshot and fast-start compatibility checks remain unchanged, and validation errors still block invalid deployments.

## Offline evidence

At the checkpoint, 207 focused tests passed in 9.07 seconds. The 23 new CPU cases cover positive resource bounds, exact queue and pool qualification, CPU/RAM capacity minima, no CPU/GPU mixing, no GPU snapshot settings, no synthetic GPU resources, KEDA zero/fixed-hot rendering, native Ready/Cold route publication, actual controller zero bootstrap and autoscaler handoff, and CRD shape.

After the approved onboarding correction, the same combined suite passed **208 tests in 8.88 seconds**, including 24 CPU cases and explicit warning-versus-error regressions. Owned-source mypy remained clean. The qualification flag remains false until measured, while an explicit zero floor can now obtain that measurement.

The regression suite also includes the existing model-deployment controller, bridge, dynamic routes, startup-retention query, runtime bindings, fast-start, mutation, and publication tests.

```bash
cd k8s-inference/components/control-plane
uv run pytest tests/test_cpu_model_deployment.py tests/test_model_deployment.py \
  tests/test_model_deployment_mutation.py tests/test_model_deployment_publication.py \
  tests/test_model_deployment_controller.py tests/test_model_deployment_bridge.py \
  tests/test_deployment_runtimes.py tests/test_fast_start.py \
  tests/test_serving_startup_retention.py tests/test_dynamic_routes.py -q
```

Ruff passed on owned source/tests; mypy passed on the five owned source modules using the existing missing-external-stub allowance. Helm strict lint passed with synthetic image/digest/HTTPS values and the required `fs2-system` namespace. A first default-values lint failed because deployment-specific image/digest/URLs were deliberately absent; no live configuration was supplied or changed. `git diff --check` passed.

Hard-coded legacy fixture identities retained:

- GPU spec: `sha256:092bab27467b2a92ccfba642ba13cbd2896bdbde3e85080ebf687d105987f000`.
- GPU envelope: `sha256:27e7554358ce022697f1b35df8c39a8e46586a9232f06bcfb2374067bb9e407d`.

## Remaining release gate

The root agent owns Terraform/catalog integration and deployment. Following that release, test distinct public Apps, CPU scale-from-zero, two useful requests, dynamic settings, owner history and cleanup. Direct runtime CPU/H100 qualification is recorded separately in [the aging acceptance report](README.md); it is not a substitute for public App cold-start or routing acceptance. No additional permissions were needed for the offline implementation.
