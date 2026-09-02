# Read-only H100 inventory — 2026-09-02

This is preparation evidence, not an acceptance baseline or readiness claim.
The cluster changed throughout the `22:50Z`–`23:00Z` observation window. All
cluster reads used kubeconfig
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig` and
explicit context `k8s-inference-h100`. No Secret data was read.

## Target and capacity

- Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`
- Project/region: `project-e00rene` / `eu-north1`
- Kubernetes: `v1.35.6`
- Two Ready capacity-block nodes in pool `h100-reserved-8x`, 8 × H100 80 GB
  each:
  - `computeinstance-e00m0hsph76ajt9sdb`, UID
    `cda2bfb1-45af-435e-acfc-7a588f4529ee`
  - `computeinstance-e00p3acr87k9k4mckj`, UID
    `5700ea19-0b46-4020-afa5-c8f1a105dcdd`
- One Ready preemptible 1 × H100 node in pool `h100-1x`:
  `computeinstance-e00phxdecf401f6rq5`, UID
  `0a2a9c6d-c5fe-4194-93b9-510281c6732b`.
- One system CPU node and one reference-data CPU node were Ready at the final
  observation. The reference-data node appeared during inventory.
- GPU driver: `580.159.04-1ubuntu1`. GPU nodes report no local NVMe and snapshot
  eligibility false.

## Live workload motion

The snapshot cannot be used as a quiescent before-state:

- The existing Qwen hot replica was Running on one reserved H100 with image
  `vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
- `fs2-academic-poc/fs2-image-smoke-rfdiffusion` changed while inventory was in
  progress and was Running outside Kueue at `23:00Z`, image
  `rfdiffusion@sha256:56a5ed22e39f41284658c1a5840cb0286cba419f22cd7f4c2f7905ea5f803396`.
- AlphaFold3 academic loader Jobs had succeeded with image
  `alphafold3@sha256:bead2e68627c1aa7d5fa80243b1164a18160f48cf3a1867090d72ff2b9270e37`.
- Complexa v1–v4 attempts failed; v4 reported a conflicting stale
  `kueue.x-k8s.io/workload` annotation. At `23:00Z`, v5 was Running on the
  preemptible H100 with image
  `proteina-complexa@sha256:d3f3c9bc5a2285b09932eb05a57ef73da3201bc69b77462420c0d42a0aaa91d8`.
- Academic PyRosetta installation and Complexa artifact staging were active
  sibling tasks. They must be provenance-resolved and preserved.

These ad-hoc Jobs are not admitted ModelDeployments and do not establish model
readiness.

## Shared platform provenance and rollback anchors

| Component | Current revision | Current image digest | Previous distinct digest |
| --- | ---: | --- | --- |
| `fs2-serve-control-plane` | 24 | `sha256:e40c69a6363d1b873780869e3d8301121c7da410435f5d76481986cf2994b571` | revision 22, `sha256:ea053c96e4c072b390f4c43fee58c6c3cb626f0f365a96ed13a0fa2b3c0f258f` |
| `fs2-serve-control-plane-model-controller` | 11 | `sha256:e40c69a6363d1b873780869e3d8301121c7da410435f5d76481986cf2994b571` | revision 9, `sha256:ea053c96e4c072b390f4c43fee58c6c3cb626f0f365a96ed13a0fa2b3c0f258f` |
| `fs2-serve-control-plane-admin-console` | 11 | `sha256:75bf54476e194a3a26732620b59a850e37921fa7ada3c7080b0008dfecdf8d6a` | revision 10, `sha256:e34afb23ca2bc108fad81483458349a73942153fde745f16049019e65d4b0b31` |

All current replicas were Ready. Rollout annotations do not identify a source
commit. The private deployment contract records admin source commit
`2916dc4dc3412af05756ecce1b5edbfcd79102fe`, but it carries no corresponding
control-plane source commit. Therefore current-image-to-source ancestry is not
fully proven and deployment remains blocked.

Only Qwen and Cosmos ModelDeployments exist. No requested cancer model is an
admitted ModelDeployment.

## Queue state

- ClusterQueue `inference-accelerators`, UID
  `927bb1cc-804c-4449-bdc3-de855f81faf7`, is Active and uses
  `BestEffortFIFO`.
- LocalQueue `fs2-models/inference-models`, UID
  `7e966947-e726-459a-ae04-e43fa9162af6`, is Active.
- There is no cohort. Within-queue preemption, cohort reclaim and cohort borrow
  are all `Never`.
- WorkloadPriorityClasses are only `batch=-100`, `standard=0` and
  `interactive=100`. `presentation=1000` and `bulk-backfill=-100` from the
  reviewed contract are not deployed as such.
- There are no separate tenant LocalQueues, ResourceQuotas or LimitRanges.

Priority, fair-sharing, borrowing/reclaim and preemptible-recovery acceptance
cannot run against this topology.

## Storage and telemetry

- Shared storage is the RWX `csi-mounted-fs-path-sc` pattern. Existing model and
  academic PVCs are 128 GiB; database PVCs are 100 GiB RWO and Loki is 50 GiB
  RWO.
- A reference-data CPU node existed, but no `fs2-reference-data` namespace or
  PVC was present at the final observation.
- Prometheus has 10-day retention but no persistent storage specification.
  Loki is persistent. No Tempo/Jaeger/raw trace backend exists.
- OTel sends traces through spanmetrics without raw retention.
- DCGM is Ready on all GPU nodes, but the ServiceMonitor uses a 30-second
  interval, `honorLabels=false` and no corrective relabeling. Canonical Pod
  labels identify the exporter; real workload identity is in `exported_*`
  labels.
- Available FS2 metrics include operation totals, durations and estimated GPU
  seconds. Exact load/restore/compile/warmup/active/idle/grace/drain/teardown
  intervals are absent.

This prevents chargeback-quality lifecycle reconciliation.

## Public read-only smoke

Gateway address `89.169.99.188` was Accepted/Programmed. Its Let's Encrypt
certificate has IP SAN `89.169.99.188`, valid from `2026-09-01 14:20:45Z` to
`2026-09-08 06:20:44Z`.

- `GET /readyz` → `200` and reported two models with healthy admission workers.
- Anonymous `GET /v1/models` → `401`.
- Anonymous `GET /mcp` → `401`.
- Anonymous `GET /admin/api/v1/models` → `401`.

These prove basic TLS/readiness/auth boundaries only. No authenticated
scientific API, MCP or admin acceptance was attempted.

## Reproduction

```bash
python3 models/cancer-immunotherapy/live-e2e/acceptance_harness.py \
  inventory \
  --kubeconfig /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig

KUBECONFIG=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig \
kubectl --context=k8s-inference-h100 get deployments,pods,replicasets \
  -n fs2-system -o json

KUBECONFIG=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig \
kubectl --context=k8s-inference-h100 get \
  clusterqueues.kueue.x-k8s.io,localqueues.kueue.x-k8s.io,\
workloadpriorityclasses.kueue.x-k8s.io -A -o json
```

No Terraform command, deployment, model request, GPU Job submission, deletion,
patch, apply, scale, secret read or Forge access was performed.
