# Scientific scheduling live acceptance

This directory prepares, but does not perform, shared-cluster contention tests.
`render_acceptance.py` reads the exact Terraform scheduling output and emits
only namespaced, task-labelled Jobs. It never creates or changes a
ClusterQueue, LocalQueue, ResourceFlavor, WorkloadPriorityClass, node group, or
control-plane deployment.

Do not run these steps until root has reviewed one integrated Terraform plan
that includes the currently deployed control-plane source and the task's queue
policy. The live cluster is last-deploy-wins. A queue-only branch must not
replace a shared control plane, and an acceptance run must not invent quota or
assume idle GPUs.

The integrated foundation must first report its pinned JobSet v0.12.0 contract
ready on Kubernetes 1.33-1.35. JobSet publishes upstream E2E coverage through
1.34; 1.35 additionally requires the exact FS2 Kind and live qualification
recorded in modules/jobset-controller/QUALIFICATION.md. Managed patch upgrades
within those minors are accepted. Kueue's JobSet integration flag alone does not
install the JobSet CRD or controller. The workloads stage fails closed when
that readiness contract is absent, so true-gang work cannot target a
nonexistent API.

## Raw AlphaFold 3 data-stage configuration

The raw data stage is CPU-only, reads the shared reference databases, and needs
16 CPU and 64 GiB in one Pod. Three things must line up before it can run, and
Terraform refuses the plan when any of them does not:

| Input | Required value |
| --- | --- |
| `deployment.scheduling.academic_raw_data_stages` | `true` |
| `deployment.scheduling.core_capacity` | exact aggregate schedulable cpu/memory of the pools backing this Kueue installation |
| reference CPU pool `schedulable_capacity` | at least `16000` millicores and `65536` MiB, measured |
| reference `queue.nominal_cpu` / `nominal_memory` | at least `16` and `64Gi` |

The pool must be sized from measured allocatable capacity, not from a machine
preset's nominal size: a nominal 16 vCPU / 64 GB node has less than 16 CPU and
64 GiB available to a Pod once the kubelet and system reservations are taken,
so it cannot hold this stage. A 32 vCPU / 128 GB class pool with conservative
declared schedulable capacity is the smallest honest choice.

The currently deployed reference pool is 8 vCPU / 32 GB with measured
`7000m`/`28672Mi` and a 6 CPU / 24 GiB ClusterQueue quota. That is correct for
the pipeline stager and cannot admit this stage, so raw mode stays off until
the pool is replaced. Growing it is a node-group replacement, and the canonical
16 CPU / 64 GiB request is a floor: an operator override may raise it and can
never lower it, so the stage cannot be silently shrunk to fit a smaller pool.

## Required inputs and preflight

Create a mode-0700 run directory outside the repository. Record the exact
project, region, cluster ID, kubeconfig/context, source commit, applied
scheduling-contract SHA-256, Kueue version, current control-plane image digest,
GPU type, and capacity type. Capture these read-only objects before submitting
work:

```bash
git rev-parse HEAD
kubectl --context "$FS2_ACCEPT_CONTEXT" config current-context
kubectl --context "$FS2_ACCEPT_CONTEXT" get nodes -o json
kubectl --context "$FS2_ACCEPT_CONTEXT" get resourceflavors,clusterqueues,workloadpriorityclasses -o json
kubectl --context "$FS2_ACCEPT_CONTEXT" get localqueues,workloads,jobs,pods -A -o json
kubectl --context "$FS2_ACCEPT_CONTEXT" -n fs2-system get deployments,replicasets,pods -o json
kubectl --context "$FS2_ACCEPT_CONTEXT" -n kueue-system get deployment,pods -o json
```

Obtain `scheduling_contract_ref` from the state for the reviewed workloads
plan, fetch that exact ConfigMap, extract `kueue-scheduling.json`, and verify
its bytes against the reference's `sha256`. Do not use a local plan output as
evidence of the applied policy.

The holder image must be available from the approved regional registry, pinned
by digest, runnable as a non-root user, and contain POSIX `/bin/sh`, `date`, and
`sleep`. Record its repository, digest, source commit, scan/SBOM receipt, and
registry region. The holder allocates the configured extended accelerator
resource; it does not claim model semantic validation.

## Deterministic manifests

Every render is offline and stable for the same arguments. Replace the example
identities only with values present in the applied contract:

```bash
python3 acceptance/scientific-scheduling/render_acceptance.py \
  --contract "$FS2_ACCEPT_CONTRACT" \
  --scenario victims \
  --run-id "$FS2_ACCEPT_RUN_ID" \
  --image "$FS2_ACCEPT_IMAGE" \
  --model-id "$FS2_ACCEPT_MODEL" \
  --tenant-a "$FS2_ACCEPT_TENANT_A" \
  --queue-a "$FS2_ACCEPT_BULK_QUEUE" \
  --pool-id "$FS2_ACCEPT_POOL" \
  --parallelism "$FS2_ACCEPT_GPU_CEILING" \
  > "$FS2_ACCEPT_RUN_DIR/victims.json"

python3 acceptance/scientific-scheduling/render_acceptance.py \
  --contract "$FS2_ACCEPT_CONTRACT" \
  --scenario preemptor \
  --run-id "$FS2_ACCEPT_RUN_ID" \
  --image "$FS2_ACCEPT_IMAGE" \
  --model-id "$FS2_ACCEPT_MODEL" \
  --tenant-a "$FS2_ACCEPT_TENANT_A" \
  --queue-a "$FS2_ACCEPT_PRIORITY_QUEUE" \
  --pool-id "$FS2_ACCEPT_POOL" \
  > "$FS2_ACCEPT_RUN_DIR/preemptor.json"
```

Use `fairness` with two distinct reviewed LocalQueues and tenants. Use
`partial-admission` with both `--parallelism` and a smaller
`--minimum-parallelism`; the renderer adds the upstream Kueue
`kueue.x-k8s.io/job-min-parallelism` annotation. Use `scale-zero` only after
read-only provider evidence proves the selected preemptible pool is at zero.
That scenario adds the provider-neutral pool-ID node selector so another
ResourceFlavor cannot satisfy the test.

Review each JSON file before applying it. The names, Operations, Workloads, and
Attempts are deterministic; all resources carry
`fs2.nebius.ai/acceptance-run=<run-id>`. Apply only one phase at a time with the
exact context. Do not edit the generated manifest after review.

## Scenario order and pass conditions

1. **Admission wait:** submit a Job whose request cannot currently fit. It must
   remain suspended with a pending Kueue Workload, then receive
   `QuotaReserved` and `Admitted` only after capacity is released. Queue
   latency is `QuotaReserved.lastTransitionTime` minus **this attempt's**
   durable `queued_at`, never the Workload's `metadata.creationTimestamp`: a
   retry, a requeue, or a preemption starts a new attempt whose clock resets,
   and a creation timestamp would silently accumulate every earlier wait.
   Record `QuotaReserved` and `Admitted` as separate observations, and record
   the reservation tuple as soon as `status.admission` appears, because Kueue
   clears it when the reservation is released.
2. **Priority and graceful victims:** admit the bulk victims first, then submit
   the presentation preemptor. The observed Workload must use the rendered
   WorkloadPriorityClass. When the reviewed queue's preemption policy permits
   it and Fair Sharing selects it as a valid victim, Kueue must record the exact
   preemptor/preemptee reason and the victim Pod
   log must contain `fs2-acceptance-graceful-termination`. A raw holder proves
   signal/grace handling only; checkpoint/resume requires an integrated real
   scientific Job and artifact receipt.
3. **Borrow and reclaim:** prove the victim ClusterQueue is above its nominal
   floor, then submit a higher-priority workload to a queue reclaiming its
   nominal quota. Record both ClusterQueue usage snapshots and all Kueue
   preemption events and fair-share status. A workload that only fits within
   its own unused quota is not borrowing/reclaim evidence, and numerical
   priority alone is not a cross-ClusterQueue eviction guarantee.
4. **Multi-tenant fairness and starvation protection:** submit equivalent
   IndexedJobs to two LocalQueues in the same ClusterQueue. Repeat bounded
   rounds long enough to cross the configured admission-usage sampling
   interval. Retain each LocalQueue's `status.fairSharing` and admission order;
   a single coincidental ordering is not evidence of weighted fairness.
5. **Partial admission:** leave residual quota strictly between the requested
   and minimum parallelism. Record the original Job parallelism, the admitted
   PodSet count, and Kueue's patched Job parallelism. Independent scientific
   shards remain separate Jobs and do not use this optimization.
6. **Scale from zero:** with the chosen preemptible node group at zero, submit
   the forced-pool Job. Record provider node-group size/timestamps, the joining
   Node UID and labels, Pod scheduling time, and return-to-zero time after the
   exact Job is deleted. Do not touch B300 for the H100 program.
7. **Preemptible loss and retry:** use a real integrated scientific request,
   not the holder, and only in an approved fault-injection window. Record the
   exact victim Node UID before root authorizes its interruption. The same
   Operation/Batch/Workload IDs must survive, a new Attempt ID and its own
   `queued_at` clock must be persisted, the prior attempt's quota-reservation
   and admission facts must remain immutable, actual pool/flavor admission must
   be recorded per attempt, and a
   semantically valid committed result must finish the run.

For every phase, continuously capture the exact Job, generated Kueue Workload,
Pods, Nodes, Events, LocalQueue and ClusterQueue status, and provider node-group
state. Store one receipt per observation using `evidence.schema.json`. Record
`null` only where the schema permits it; do not infer missing timestamps from
wall-clock observation.

## Cleanup

Delete only the reviewed manifest files from the exact context, then prove zero
remaining Jobs and Workloads with the acceptance-run label. Wait for any
task-created preemptible capacity to return to its prior size and record that
provider evidence. Do not delete shared queue policy, controller objects,
unrelated tenants, or the regional holder image as part of workload cleanup.

The test is incomplete until the receipt records zero remaining task Jobs,
Workloads, and temporary nodes.
