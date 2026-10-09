# Existing four-L40S pool: scheduling activation

Scope: enable **GROMACS independent one-GPU requests** on the existing `l40s-4x`
pool, retaining all other model placements, execution images and cloud bounds.
This is not a qualification of four-GPU MPI, other Apps, or CUDA snapshots.
The previous [REST test](../gromacs-concurrency-20261003/README.md) demonstrated
13 simultaneous GPUs; its evidence is retained unchanged.

## Configuration and deployment

Target: `project-e00rene`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, eu-north1.
Existing Terraform-owned node group `mk8snodegroup-e00mjdda8tb8apx53h` has one
`gpu-l40s-d / 4gpu-128vcpu-768gb` node, minimum/maximum **1/1**, four L40S GPUs.
No node group, quota, API key, tenant, bucket or client is created.

The infrastructure state already included this pool, but the scientific
scheduling contract and Kueue did not. `activate.py` performs an additive,
baseline-tested configuration migration:

1. Verify the exact Ready, schedulable L40S node and its allocatable resources.
2. Add `inference-l40s-4x` ResourceFlavor, four GPU quota units with the observed
   CPU/memory capacity, and a zero-reservation entry in the existing Cohort.
3. Extend only GROMACS's eligible pools. Put the new flavor first while preserving
   every old flavor's order/quota and every other model's eligible pool set.
4. Validate through the actual scheduling resolver and Kubernetes server dry-run.
5. Create a content-addressed immutable ConfigMap and roll only its Deployment
   volume reference and matching SHA-256 environment value. No image change.

The node reports 127,900 mCPU, 751,132 MiB memory and 283,687 MiB ephemeral storage.
Each independent GROMACS request still asks for one GPU, eight CPU cores, 16 GiB
memory and 64 GiB scratch, plus its existing artifact collector. These values
fit four simultaneous requests on the node; live evidence must establish actual
execution and artifact integrity separately.

The API image stays `sha256:6a2876b380f43717ea37cb21dd4504af5884c9a305c486562a42ef103420f946`.
The GROMACS image stays `sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643`.
New scheduling ConfigMap: `fs2-scientific-scheduling-10e4fbdbd1b6`;
SHA-256 `10e4fbdbd1b66719e391c2a0ee917c27f7d4f48f1b091b39cca1c0bb5e368dcb`.
Original ConfigMap is retained for rollback.

Private operator input `/home/tux/.local/state/k8s-inference-dual-acceptance/h100/terraform.tfvars`
now includes the already-provisioned pool, its measured
`deployment.scheduling.accelerator_schedulable_capacity`, GROMACS's
`model_eligible_pool_ids` and `default_queue_pool_order`:

```hcl
# Merge into the existing deployment object; do not replace other settings.
accelerator_pools = {
  # Keep all other existing pools.
  "l40s-4x" = {
    platform          = "gpu-l40s-d"
    preset            = "4gpu-128vcpu-768gb"
    accelerator_class = "nvidia-l40s-48gb"
    gpus_per_node     = 4
    gpu_memory_gb     = 48
    host_architecture = "amd64"
    capacity_type     = "regular"
    min_nodes         = 1
    max_nodes         = 1
    driver            = { mode = "managed", preset = "cuda13.0" }
    local_nvme        = false
  }
}
```

The generic Terraform renderer already supports this pool shape. This change
does **not** replay a full Terraform/Helm release: the saved Helm image and some
generated workload inputs predate recent production releases. Replaying them
would undo sibling work. A future broad apply must reconcile the complete live
release first, not use this pool change as permission to roll it back.
`helm-overlay.json` captures the scheduling pointers for that reconciliation;
it is not a complete safe Helm values file. The private source input was formatted;
no claim of a full no-drift Terraform apply is made here.

## Reproduction and rollback

Use the control-plane virtual environment. Preparing without `--apply` writes
private before/after documents and validates them server-side. Review those
documents, then repeat with `--apply`. A changed baseline fails its JSON Patch
test rather than overwriting a concurrent change. Applied migrations are not
blindly rerunnable: inspect current identities before preparing another.

```bash
components/control-plane/.venv/bin/python acceptance/l40s-pool-20261003/activate.py \
  --output /private/reviewed-pool-activation
# After review: repeat the same command with --apply.
```

Rollback only the scheduling pointer/digest using the saved
`deployment.rollback.json`; it tests the expected current values. Retain the
original ConfigMap. Wait for already-admitted new-pool operations to drain before
removing its flavor/quota; do not delete running jobs or blindly restore an old
Deployment/Helm release. Extra empty flavor registration is harmless during
rollback and is not authority to destroy the node group.

## Acceptance

Two public REST bursts of sixteen independent 1 ns simulations, existing
`system/qa` identity only, same workload/semantic validator as the previous
test. Campaign `fs2-gromacs-l40s4x-20261003`, seed base `20262003`; distinct inputs,
operation idempotency keys and system workspace prefixes. Customer keys,
concurrency settings, data and clients are untouched. Internal QA concurrency is
temporarily sixteen and restored to its original two after draining.

Raw receipts: `/home/tux/secure-handoff/fs2-l40s-pool-20261003/`.
[results.json](results.json) records **32/32 independently validated results**,
including eight on the newly enabled pool. All four L40S devices on that node
ran GROMACS simultaneously. Native trajectories, energies, seeds, command exit
codes, artifact hashes and resource release passed for both cohorts. All 209
public API probes succeeded. Internal QA concurrency was restored to two after
all operations drained at 11:04 UTC. No customer identity or limit changed.

The campaign reached **13 actual simultaneous GROMACS devices**, not sixteen.
The existing L40S single-GPU autoscaler grew from eight to ten Ready nodes within
its unchanged bounds. [deployment.json](deployment.json) and
`verify_deployment.py` independently confirm that every old quota and every
unrelated Deployment setting was preserved, with three Ready API replicas.
Five activation tests, five concurrency-runner tests and `terraform validate`
passed. Source activation commit: `e85e00674eff99f3183cf61c298b29b4efdd32ca`.

## Remaining placement limitation

Pool eligibility and GPU availability are different. In this configuration,
Kueue admits six requests to `h100-ondemand-1x` and two to `h100-reserved-8x`,
but existing workloads leave only four and one physical GPU slots respectively.
The three excess requests remain pinned to their admitted H100 flavors while
spare *potential* L40S scale-out capacity cannot satisfy their selectors. Adding
the four-L40S pool does not correct that pre-existing quota/occupancy mismatch.
The warm repeat observed thirteen actual GROMACS GPU processes, not sixteen.

Do not fix this by raising cloud limits, modifying a customer's key, evicting
other Apps, or describing Kueue admission as actual execution. Follow-up needs
reconciled admission accounting/placement for the existing non-Kueue GPU
occupancy and another sixteen-device measurement. This activation leaves those
other workloads and their quotas unchanged.
