# Shared Lynx / WhiteLab GPU placement — October 6, 2026

## Scope and decision

Owner requested Lynx eight parallel jobs, accepting less during capacity loss,
and WhiteLab four to eight. Implemented best-effort shared admission rather than
dedicated nodes. This is not a claim that every platform feature is ready or that
eight end-to-end customer requests were qualified concurrently.

| Workload | Placement | Observed resource-fit concurrency |
| --- | --- | --- |
| Independent MD | L40S first; qualified H100 fallback | Eight alongside single-cell work |
| Standard scVI/scANVI (128 GiB RAM) | Single-GPU H100 first | Eight alongside eight MD-shaped jobs |
| Atlas scVI/scANVI (256 GiB RAM) | Full H100 hosts, one GPU per job | Four; eight did not fit scratch reservations |

Lynx keeps its existing shared, non-expiring eight-operation key. WhiteLab is not
onboarded; `whitelab` is the prepared tenant route, not a fabricated account.
Give its intended key an eight-operation limit during authorized onboarding and
verify that identity's access then. Key concurrency includes queued operations,
not MPI ranks or umbrella windows. Submissions above a key's active-operation
limit receive a retryable 429; there is no unlimited per-key backlog.

LocalQueues `lynx-md` (weight 2) and `whitelab-single-cell` (weight 1) share
`inference-accelerators`. Historical fair ordering uses GPU allocation, not raw
RAM bytes. CPU/RAM/RDMA admission quotas and per-node fit remain enforced. A
weight is neither a guaranteed floor nor a hard concurrency limit; a running
same-priority long simulation is not evicted to satisfy the planning target.

## Exact deployment

- Source branch `agent/fs2-scvi-whitelab-20261006`, initial policy commit
  `c5f90edcd`; prior baseline `dff4005d6`.
- Project `project-e00rene`, eu-north1, cluster `mk8scluster-e00j5z9te7x5dd9g6a`.
- Explicit context `nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`.
- Observed 42 Ready GPUs: 12 L40S, 28 H100, 2 H200. H200's live quota was
  preserved but it was not added to the qualified MD/scVI model pool map.
- New immutable scheduling ConfigMap `fs2-scientific-scheduling-75bdc12794d2`,
  SHA256 `75bdc12794d274e890a6489c82496f9f02102be1dc0a455c651ea486ea690b4b`.
- API ready replicas 3/3; execution contract `fs2-scvi-execution-7e5800939d11`
  and all worker images unchanged.
- Backend image SHA256 `f64cd4b39a6eaf41e1762837e0a878ee7a78307f973ef6a822a29b118b5510df`.
- GROMACS image SHA256 `ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`.
- scVI/scANVI image SHA256 `063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246`.
- Kueue 0.17.8, release `fs2-r927c465c6d-kueue`, namespace `kueue-system`.

No new nodes, cloud quota changes, customer credentials, scientific runtime
changes or customer tests. Operator Terraform scheduling inputs were updated
at `/home/tux/.local/state/k8s-inference-dual-acceptance/h100/terraform.tfvars`.
The public [overlay example](../../examples/scheduling-lynx-whitelab.tfvars.json)
and [operating procedure](../../operations/shared-admission/README.md) reproduce
the policy. Root Terraform's budgeted-resource validation now includes managed
RDMA when present. A stale full workload-stage apply was deliberately not used:
it would overwrite unrelated newer serving/operator state. The scoped rollout
uses live baselines and exact test-and-replace patches; no claim of a fresh
two-cluster Terraform acceptance is made here.

Kueue's single-replica rolling update initially lacked 500m CPU of surge room on
the eligible CPU node. With no customer GPU jobs, the rollout used a temporary
zero-surge strategy and then restored the original 25%/25% strategy. Controller
returned to 1/1 Ready. No GPU jobs or nodes were restarted. Future Kueue upgrades
still need CPU surge headroom or an explicitly planned controller-only restart.

## Qualification

- 18 planning/harness unit cases; 27 Terraform scheduling-module tests; root
  Terraform validate/fmt; 40 existing backend placement/key/namespace regression
  tests. Exact contract resolver checked 40 tenant/model/class routes and three
  whole-Pod resource shapes.
- Dedicated **system-owned temporary** admission queues exercised real GPU
  allocation and `nvidia-smi`, not scientific computations. Eight L40S-shaped MD
  jobs and eight H100-shaped standard single-cell jobs ran simultaneously. One
  extra per queue waited, then all 18 completed after capacity release. Existing
  customer queues and key usage were not used for this test.
- Atlas eight-plus-eight hypothesis failed: eight MD jobs and four atlas jobs
  ran, four atlas Pods waited with `Insufficient ephemeral-storage`. Each full
  H100 node reports 297,467,776,530 bytes (~277 GiB) allocatable ephemeral space;
  only two 128 GiB reservations fit. This is not GPU exhaustion or a basis for
  silently reducing storage requests. Eight-atlas concurrent execution is not
  qualified. Acceptance evidence retains this negative result.
- The corrected eight-MD-plus-four-atlas shape passed: 12 simultaneous running
  Pods, two additional workloads queued, and all 14 completed after release.
  Independent final checks found no remaining owned test Jobs, Pods, Workloads,
  LocalQueues, ClusterQueues or gate ConfigMaps. Only test resources were
  removed; receipts, exported results and rollback snapshots remain recoverable.
- Real mixed API/MCP qualification uses the existing `system/qa` key, limited
  to two operations; no customer key or data. Cohort A submits GROMACS by REST
  and scVI/scANVI by MCP. Cohort B reverses the protocols. Replays must reuse the
  same operation; terminal results, ownership-authorized artifact downloads,
  byte counts and SHA256 digests are checked.
  All four operations passed on the final contract. Both GROMACS attempts used
  `l40s-4x`; both scVI/scANVI attempts used `h100-ondemand-1x`. Each GROMACS run
  verified 38 manifest entries and all ten commands; each single-cell run
  verified 41 entries and all 584,944 aligned output rows. The four completed
  operations no longer have Kubernetes GPU Jobs; their results remain retained.
  To replay a retained run, use its recorded idempotency key explicitly with
  `qualify_hosted.py --idempotency-key`, or poll its `--operation-id` without
  resubmitting. The original single-cell default prefix remains backward
  compatible; cohort A recorded an explicit `scvi-scanvi-hosted-...` prefix
  during initial harness development.
- GROMACS input is the public MPINAT `benchstc` fixture, three 10,000-step
  repeats plus energy extraction (ten commands). This checks mixed-service
  regression, not sustained performance of the customer's different simulation.
- scVI/scANVI uses the public 584,944-cell HLCA fixture, two scVI plus two scANVI
  epochs. Export validation checks every cell, finite latent values, normalized
  probabilities and matching labels. This is not convergence/biological-accuracy
  qualification; the longer preceding model qualification is separate.

Machine-readable final results and cleanup evidence are in `results.json`.
An initial harness server dry-run rejected non-cohort borrowing fields before
any GPU Jobs were submitted; the harness was corrected. A later draft plan with
a mistyped image digest was not applied. Neither is counted as successful GPU
evidence, and the executed cohorts all use the full qualified digest above.
Private raw receipts, payloads, results, immutable artifacts and rollback patches:
`/home/tux/secure-handoff/fs2-shared-admission-20261006/` (not a customer handover
location). Existing QA bucket exports are retained, not deleted after testing.

## Remaining reliability issue — not concealed by successful jobs

Before the scheduling API rollout, `/readyz` returned an observed 503; later
samples returned 200 with scientific reconciliation failure counters still 1–2.
Two old internal failed scVI operations repeatedly fail diagnostic publication
with `failed diagnostic receipt differs from its frozen attempt`. Their old
receipts omit/null fields required by the current strict reader. New mixed jobs
succeed, but that does not prove unattended backend readiness.
A separate final single curl probe timed out after 20 seconds; its cause was
not established. Subsequent direct HTTP checks returned 200 twice and all three
API replicas were Ready, but still reported one reconciliation failure. Do not
silently discard the timeout or attribute it to this bug without evidence.

Prepared, not started, Task Deck follow-up:
`fs2-scientific-failed-receipt-isolation-r20261006`. It records exact operation
and artifact IDs, requires bounded per-operation handling, and preserves strict
artifact correctness and real infrastructure readiness failures. No database
rows or old receipts were edited to make the error disappear. Resolve this
before claiming unattended WhiteLab customer readiness.

## Rollback

Private `plan-r1` holds the exact API, ClusterQueue and controller-config rollback
patches. Restore the prior immutable scheduling CM (`1a3431c6b530`), controller
weights and flavor order, roll/verify the affected controllers, and drain new
lanes before removing them. Preserve active Jobs and all evidence. All live
quota values, unrelated queue bindings and existing resource flavors were
preserved by this rollout.
