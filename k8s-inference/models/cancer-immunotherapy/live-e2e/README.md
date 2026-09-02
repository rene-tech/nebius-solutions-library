# Cancer-immunotherapy live acceptance preparation

This directory prepares the final H100 acceptance wave without claiming that it
has run. The checked-in plan is bound to reviewed `main` commit
`c83b97c6f18b09f13b0623d20f6436398337085f`. It covers the nine requested model
identities, seven contention/recovery scenarios, seven startup levels, exact
telemetry equations, externally reachable surfaces, rollback and UID-fenced
cleanup.

The current decision is **blocked for execution**. `preflight` must remain
nonzero until reviewed runtime, adapter, controller, scheduling, telemetry and
surface contracts are integrated and the plan is deliberately rebased to their
new digests. An empty profile set, a candidate receipt or a UI fixture can never
make a lane runnable.

## Safe commands for this wave

```bash
./models/cancer-immunotherapy/live-e2e/run_checks.sh

python3 models/cancer-immunotherapy/live-e2e/acceptance_harness.py preflight

python3 models/cancer-immunotherapy/live-e2e/acceptance_harness.py \
  render --run-id prep-20260902

python3 models/cancer-immunotherapy/live-e2e/acceptance_harness.py \
  inventory \
  --kubeconfig /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig
```

`inventory` has an allowlist containing only `kubectl get`-class reads and emits
selected metadata, image identities, node capacity and Kueue objects. It never
reads Secret data, Pod environment variables, command arguments or mounted
configuration. `render` creates deterministic operation/attempt identities and
canonical labels, but every case is explicitly
`blocked-until-preflight-ready`; there is no submit or execute command in this
preparation version.

`validate-artifacts` is intended for the final wave. It requires an exact
`semantic-validation-receipt.json`, an immutable runtime image digest, input and
output manifest digests, all model-specific oracle checks, and local corruption
prechecks. File presence, parseable coordinates, CSV rows and finite JSON values
are deliberately insufficient by themselves.

## Requested model matrix

| Model | Exact source revision | Oracle | Current gate |
| --- | --- | --- | --- |
| AlphaFold3 | `85c4d20505fd5cef05eac22b534d4e793971ae69` | Frozen ubiquitin and barnase-barstar; entity counts, topology/RMSD, iPTM/ranking/clash and finite values | Tenant-private terms/artifact receipt, reference data, exact fixtures and thresholds |
| BindCraft | `7cd4ace1b7407adf66a50dfefa47de2270f5e4a9` | PD-L1 target preservation, binder constraints, hotspot contact, real PyRosetta metrics and seed diversity | Academic PyRosetta receipt and exact numeric validator |
| BoltzGen | `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0` | Exact output count, refold consistency, composition/unresolved-residue and interface confidence | Executable adapter/image/artifacts, thresholds and long H100 stall-regression case |
| ESMFold2 | `827ec128e4cdaf80f7d6f95fb367a08980b34918` | Ubiquitin count/topology, pLDDT/pTM/PAE and repeatability | Exact weight/image tuple, fixture bytes and numeric bounds |
| ESMFold2-Fast | `827ec128e4cdaf80f7d6f95fb367a08980b34918` | ESMFold2 oracle plus backbone parity and paired speedup | Exact Fast weights, adapter and parity/latency bounds |
| mosaic | `70fec525423f5f87156a1a957b4a4048f9f8e676` | Exact component checkpoints, objective improvement, nondegenerate sequence, PD-L1 contact/refold | Frozen script/checkpoints and numeric objective/contact/refold bounds |
| Proteina-Complexa | `54058860d43444c7289873f77d3e50b5b02348cd` | Four complete stages, full atoms/sequence, accepted filter and refold consistency | AF2/RoseTTAFold3 reward assets, adapter and numeric thresholds |
| Protenix v2 | `2475421477ab414b571149ad4a875c390ff8a35d` | Exact v2 identity, 7r6r entities, confidence, DNA geometry, clashes and seed diversity | Exact v2 checkpoint is unavailable; v1 is never accepted as v2 |
| RFdiffusion upstream | `9273ef67335acaf91df0150473a274759229cdf6` | Glycine backbone, target/chain geometry, hotspots, fixed-seed repeatability and design diversity | Upstream-vs-NIM identity, adapter and numeric geometry/contact bounds |

The full oracle assertions and structural prechecks are machine-readable in
`acceptance-plan.json`. The source descriptions remain authoritative in
`../model-source-qualification.json`. Alternatives remain separate identities:
FreeBindCraft cannot satisfy BindCraft, and OpenFold3 cannot satisfy AlphaFold3.
ProteinMPNN is supporting infrastructure, not a requested readiness row.

## Current-main integration blockers

At the frozen base:

- `catalog/runtime/contracts/scientific-workload-profiles.json` contains no
  profiles. All source receipts are candidate/unqualified; five candidate
  revisions differ from the exact qualification revisions.
- There are no real cancer model declarations, executable adapters, frozen
  request fixtures or authoritative semantic validators.
- The controller is core-only: no production PostgreSQL repository,
  Kubernetes/Kueue writer, artifact service, process loop, Helm wiring or exact
  attempt-label propagation is integrated.
- Production lacks typed scientific submit/events/API/MCP routes. Scientific
  admin list/detail/model routes are fixture-only.
- The live queue is one BestEffortFIFO ClusterQueue and one LocalQueue without a
  cohort, fair sharing, borrow/reclaim, preemption, tenant queues, presentation
  priority or bulk-backfill priority.
- Generic telemetry exposes estimated GPU seconds, not a durable lifecycle
  ledger. Raw traces are not retained and DCGM identity needs exported-label
  relabeling.
- Model naming must converge without alias substitution: `rfdiffusion` versus
  `rfdiffusion-upstream`, `alphafold3` versus `alphafold3-native`, `bindcraft`
  versus `bindcraft-native`, and `esmfold2` versus `esmfold2-full`.
- Current H100 nodes have no local NVMe and advertise GPU-snapshot eligibility
  false. A shared-cache or runtime checkpoint result must not be called a GPU
  snapshot.
- There is no reviewed task-owned failure-injection contract, GPU-neutral
  cleanup receipt, or complete shared-service deployment/rollback provenance.

These are implementation gates. Separate external gates are the Protenix v2
checkpoint, AlphaFold3 terms/private parameters, BindCraft/PyRosetta academic
access, Proteina-Complexa reward artifacts and any restricted registry access.

## Final-wave sequence

1. Root identifies reviewed commits and rebases this task branch. Update every
   contract digest and source/profile binding; never copy an active task branch
   merely because it exists.
2. Re-run local tests and `preflight`. Require nine qualified profiles with
   digest-pinned images/artifacts, executable validator IDs, resolved stages and
   queue policy. Resolve external blockers explicitly; do not convert them to
   implementation failures.
3. Wait for a quiescent cluster window. Capture live Deployments, ReplicaSets,
   imageIDs, Helm revisions, model routes, Kueue state, node UIDs/GPU capacity,
   public features and sibling resources. The deploy commit must contain the
   deployed source work or use an isolated preview.
4. Execute two retained semantic cases for each admitted exact tuple, then the
   mixed burst, priority, two-tenant fairness, borrow/reclaim, authorized
   preemptible loss and serialized/preview restart scenarios. Capacity is
   derived at execution time; `16` is never hard-coded as disposable capacity.
5. Collect three attempts per startup level for exploration. A promoted L1-L4
   claim needs 20 comparable successes and zero failures. Record capacity wait,
   runtime readiness and first semantically valid committed output separately.
6. Reconcile integer-nanosecond lifecycle intervals from durable allocation
   events. DCGM corroborates activity and GPU identity; it does not define
   allocation time. Any reconciliation delta is a failure.
7. Run authenticated API/MCP/admin consistency and authorization cases, then a
   saved Terraform plan from the exact private run root. A no-op claim requires
   every managed action to be `no-op` or provider read-only `read`.
8. Cancel/acknowledge task operations, delete only deterministic task-owned UIDs
   with preconditions, wait for Pods/Jobs/JobSets/Kueue Workloads and GPU clients
   to disappear, restore temporary queue/capacity settings, and preserve only
   content-addressed artifacts and immutable receipts.

## Timing and accounting boundaries

The plan freezes request accepted, queue entered, workload created, Kueue
admitted, Pod scheduled, GPU allocated, image ready, artifact ready, restore
complete, runtime ready, first semantic result, attempt terminal, GPU released
and cleanup complete. HTTP readiness and batch semantic-runtime readiness are
not interchangeable.

The required exact equations are:

```text
allocated_gpu_ns
= active_compute_gpu
+ allocated_idle_gpu
+ resident_idle_gpu
+ grace_drain_gpu
+ reconciliation_delta_gpu

allocated_idle_gpu
= image_loading_gpu
+ artifact_loading_gpu
+ restoring_gpu
+ compilation_gpu
+ semantic_warmup_gpu
+ workflow_wait_gpu
+ scheduler_hold_gpu
+ unattributed_gpu
```

`reconciliation_delta_gpu` must be zero before display rounding. Queue and
scheduling time are end-to-end latency but are not GPU allocation unless a raw
device-allocation event proves otherwise.

## Cleanup and rollback boundary

The intended acceptance labels extend the current scientific label contract:

```text
app.kubernetes.io/managed-by=fs2-live-acceptance
fs2.nebius.ai/acceptance-run
fs2.nebius.ai/acceptance-scenario
fs2.nebius.ai/model-id
fs2.nebius.ai/workload-id
fs2.nebius.ai/attempt-id
fs2.nebius.ai/tenant-id
fs2.nebius.ai/service-class
fs2.nebius.ai/local-queue
```

Labels are for discovery only. Cleanup also requires deterministic names, the
private owner/UID ledger and UID preconditions; selector-only deletion is
forbidden. Foreign UIDs, retained caches, shared references, Qwen/Cosmos and
sibling-task resources must never be touched.

Rollback captures current and previous image digests, ReplicaSet revisions,
catalog/config digests and database compatibility before a rollout. Database
migrations are additive and are not reversed. Rollback is incomplete until the
same pre-existing public/API/MCP/admin/model smoke set passes and Terraform
again converges.
