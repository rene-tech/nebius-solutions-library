# MPI pool maximum-host feasibility

Task: `fs2-gromacs-gang-pool-cardinality-r20261005`. Source-only qualification;
the parent owns any scheduling ConfigMap/image activation and real MPI test.

## Confirmed behavior

Read-only inspection on October 5, against API image `191e2c2b…`:

- Kueue has no Topology objects, and ResourceFlavors have no `topologyName`.
- GROMACS MPI JobSets require one Pod per distinct `kubernetes.io/hostname`.
- Existing admission checks whole-Pod CPU/memory/GPU fit, not distinct-host
  cardinality. Kueue aggregate GPU quota alone does not enforce that constraint.
- `l40s-4x` has one 4-GPU node. A two-node/two-GPU-per-node request fits its
  four-GPU quota, but cannot satisfy mandatory hostname anti-affinity.
- The current two-node/**one**-GPU path deliberately uses the legacy base stage,
  whose source **and actual live** profile permit only H100 pools. It does not
  currently hit the L40S hazard. Previous 2×1 success therefore did not qualify
  arbitrary multi-node topology, nor disprove the 2×2/2×4 scheduling defect.

## Minimal fix

For new GROMACS MPI true-gang admissions only, intersect normal per-Pod fit with:

```text
configured maximum hosts = pool.capacity / per-node accelerator_count
configured maximum hosts >= requested gang_size
```

This is not an estimate from current nodes or free GPUs: the authoritative
workloads Terraform producer explicitly defines `pool.capacity` as
`pool.node.gpus_per_node * pool.capacity.max_nodes` in `stages/workloads/queue.tf`.
Unknown, negative, noninteger or inconsistent facts are rejected. A pool with
zero hot nodes remains eligible if its configured maximum can satisfy the gang.
Kueue continues to own current quota/admission and may wait for capacity.

Only GROMACS MPI currently renders this strict hostname requirement; other
gangs are not given an invented topology constraint. Single-node jobs and
already-frozen running work are unchanged. No new scheduler, live node query,
timeout, GPU quota, provider capacity or customer limit is introduced.

## Scoped existing H100 metadata correction, not capacity provisioning

The mounted scheduling contract
`fs2-scientific-scheduling-8c76afc02ad5` still describes one H100 single-GPU
host. Independent parent verification established:

- Existing node group `mk8snodegroup-e00pcb1vvq65bgvxfk` belongs to
  `mk8scluster-e00j5z9te7x5dd9g6a`, is running and declares min/max eight
  `1gpu-16vcpu-200gb` nodes.
- Authoritative private H100 `terraform.tfvars` already declares min/max eight,
  one GPU per node. Eight Ready matching nodes exist.
- The live `inference-accelerators` ClusterQueue already reserves eight GPUs,
  `127200m` CPU and `1520576Mi` memory for `inference-h100-ondemand-1x`.

`prepare_capacity.reconcile()` is a pure, idempotent review helper. Given the
captured contract and **read-only** live queue, plus that independently verified
maximum, it prepares only:

| Existing pool metadata | Before | Correct existing value |
| --- | ---: | ---: |
| `pools.h100-ondemand-1x.capacity` | 1 | 8 |
| Embedded flavor GPU quota | 1 | 8 |
| Embedded flavor CPU quota | 15900m | 127200m |
| Embedded flavor memory quota | 190072Mi | 1520576Mi |
| Same-pool `core_capacity` and `core_queue_quotas` | one-node totals | the corresponding existing eight-node totals |

It does not write a ClusterQueue or provider object. It preserves all other
pools, Wan/default drift, shared quota, existing limits and unrelated fields.
Unexpected identities or live quantities fail rather than being reconciled
silently. The parent can compose this correction with its reviewed RDMA
scheduling candidate; fresh ConfigMap and deployment guards are still required.
The normal Terraform producer already derives the corrected maximum from the
existing authoritative inputs; no second source of infrastructure ownership is
added.

Applying the pure helper to fresh read-only captures succeeded. Exactly eight
scalar fields changed, all shown above; no unrelated difference. Canonical JSON
SHA-256 before: `8c76afc02ad58653edcc4ecd61d966935a09a01a94e4724d4d03441181fe736c`;
after: `0ae63eb7efec4af40deb5c5d56d0267aeb4d92b995852625b7d55afd19c3094f`.
Source ConfigMap UID `6f723830-c413-4a08-821b-71b403744121`, resourceVersion
`110105850`; live queue UID `927bb1cc-804c-4449-bdc3-de855f81faf7`.
These identify the reviewed input, not permission to overwrite a later revision.

## Tests and remaining gate

57 focused tests passed (cardinality, execution shapes, RDMA and review-helper
tests), plus 29 existing GROMACS/scheduling/gang regressions; Ruff passed. Cases include one 4-GPU host versus two requested hosts,
four L40S hosts versus two full H100 hosts with the same aggregate GPU count,
zero hot nodes, independent queue budgets, stale maximums, invalid facts,
single-node preservation and eight single-GPU hosts.

The portable shape-conservation fixture now explicitly declares eight maximum
hosts per pool instead of pretending every pool's same 16 GPUs imply the same
number of hosts. This is a test fixture, not live capacity or quota.

Live gate: parent-coordinated metadata/image release, actual separate-node
REST/MCP admission and the bounded internal-tenant peer-loss/recovery test.
No customer key, live scheduling mutation or new inference was used by this
worker. Static maximum-host feasibility does not claim that all hosts are
currently free or that autoscaling capacity is guaranteed.
