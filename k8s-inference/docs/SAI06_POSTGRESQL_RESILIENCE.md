# SAI-06 PostgreSQL resilience and public-edge availability

This change closes the source/configuration portion of SAI-06. It makes a
dedicated retained and versioned object bucket, CloudNativePG base backups and
WAL archiving, a scheduled backup, a real recovery-cluster verification path,
three-node database placement, and a two-replica Envoy data plane mandatory in
the root deployment contract.

## Wave-1 release gate

Do not apply this branch to the shared platform until the parent coordinator
explicitly clears the SAI-09 release-lineage/provenance gate and serializes the
rollout with the other Scientific AI security remediations. The deploy branch
must first contain the exact source that produced every currently deployed
shared image and all sibling features added after this branch's base commit.

The implementation base was commit
`6d93c6caefed6a9318de3fb95599581f1ff0c556` with tree
`4f418c07ae22be22bf48e68383d923006a60d2dd`. Read-only inspection on
2026-09-16 used the retained Scientific AI project in `eu-north1` and kube
context `k8s-inference-h100`. Exact cloud identities and the explicit local
kubeconfig location are retained in the private task evidence, not this public
runbook. No Secret values or customer payloads were read.

The first snapshot found three healthy database instances on the single system
node, no `Backup` or `ScheduledBackup`, a null `firstRecoverabilityPoint`,
preferred anti-affinity, and one Envoy proxy on that same node. CNPG was Helm
chart `cloudnative-pg-0.29.0`, app `1.30.0`, at
revision 1; Envoy Gateway was chart/app `1.8.3`, revision 1. A later read-only
snapshot found the shared `fs2-serve-control-plane` release at revision 133 in
`pending-upgrade` and sibling `fs2-mindeval-workshop` at revision 1 in
`pending-install`. That volatile state is additional proof that this task must
not deploy independently.

At that later snapshot, the shared application images that must be preserved
were:

- control-plane and model-controller:
  `sha256:3e3571dd095ef932ffdd6992cc371ce689dff775d5396160cfd91eb815c014fc`;
- admin console:
  `sha256:4320eb8b1dbf9d0fce3122d6418001aac7e00f7f2ec5577c697455ab277949ce`;
- Scientific AI website:
  `sha256:b018ba613574e477aa9f83fe562da05a967f273bdf51cc3d68681993346d3c31`;
- PostgreSQL:
  `sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185`;
- Envoy proxy `distroless-v1.38.3` and Envoy Gateway `v1.8.3`.

These observations are not deployment provenance. SAI-09 must identify the
exact producing source before a shared rollout.

Independent review rejected commit `ee1e3db7afdffd3f90710990f1a333c30895d16e`.
That immutable candidate used `targetImmediate`, which stopped recovery at the
end of a base backup, and retained only 256 GiB for a 100 GiB database with
daily backups and 30-day retention. It is negative evidence and must never be
rolled out. This successor replaces both contracts; it does not rewrite the
rejected commit into a successful result.

## Implemented contract

The root facade always provisions a distinct versioned backup bucket. There is
intentionally no `enabled` input. The default recovery window is 30 days and
the default CloudNativePG six-field schedule is `0 0 2 * * *`. The bucket has
`prevent_destroy`, keeps current backup/WAL objects under Barman retention, and
expires only incomplete uploads and non-current versions outside the recovery
window. Its dedicated service account receives only `storage.object-editor` on
`postgresql/v1/*`; the secret half of its S3 key is delivered through
MysteryBox and enters the Kubernetes Secret through Terraform write-only data.

Backup capacity is retention-aware rather than a fixed 256 GiB. The enforced
minimum is:

```text
ceil(((database GiB * (retention days + 2 boundary backups))
    + (estimated daily WAL GiB * (retention days + 7 cleanup-lag days)))
    * (100 + headroom percent) / 100)
```

For the full-catalog defaults this is 5,480 GiB: a 100 GiB database, one daily
base backup, 32 GiB/day estimated WAL, 30 retained days, seven WAL/version
cleanup-lag days, and 25% headroom. The default retained bucket ceiling is
6,144 GiB. A 256 GiB value fails validation before planning. Because the live
Nebius quota response exposes current Standard-storage usage but not an
explicit ceiling, `inference-stack preflight`, `plan`, and `apply` all capture
a private, payload-free live usage receipt and require an explicit capacity and
cost acknowledgement; the receipt never claims that observed usage is a
reservation.

The database always has three instances with required hostname anti-affinity on
the regular system pool. Its `barmanObjectStore` configuration sends base
backups and compressed WAL to the dedicated store. A self-owned immediate
`ScheduledBackup` establishes the first backup without waiting for the next
cron tick and then continues on schedule.

The public Envoy data plane has two replicas, required hostname anti-affinity,
and a PodDisruptionBudget with `minAvailable: 1`. Both Envoy and PostgreSQL
remain pinned to the system pool, whose capacity profiles and explicit override
now reject fewer than three nodes. The retained private deployment input
currently requests one system node, so it deliberately fails the new source
gate. Promotion requires an intentional change to three nodes, the explicit
`three_node_ha_cost_review_acknowledged` flag, and fresh saved-plan, quota and
cost review. The remediation does not silently resize the retained system.

Restore verification is deliberately a four-apply acceptance sequence after
one exact `Backup` has completed:

1. Enable `prepare_database_restore_marker` with that Backup's resource name,
   completion time, and a new non-sensitive marker ID. The bounded source Job
   commits marker A with its WAL LSN, emits a target timestamp, waits, and then
   commits marker B with its WAL LSN.
2. Disable marker preparation, enable `verify_database_restore`, and supply the
   captured target timestamp. The temporary CNPG cluster recovers with
   `recoveryTarget.targetTime`. The verifier requires a non-null replay LSN,
   marker A at or before the replay boundary, and marker B absent. This proves
   archived WAL replay beyond the selected base backup and a bounded PITR stop.
3. Disable verification and enable `cleanup_database_restore_marker` with the
   same Backup, marker and target identity. The bounded source Job refuses
   anything except the exact A/B pair, revokes the marker-only grant and drops
   the non-sensitive marker table atomically.
4. Clear every acceptance field and apply the reviewed resource-cleanup plan.

The existing `restore_verifier` login receives SELECT only on the non-sensitive
marker table and no CREATE or write privilege. It also fails unless it has no
table or column SELECT on token, operation, audit, request/response payload,
artifact, credential, secret, or session-bearing relations. The Job has no
service account token, runs non-root with a read-only root filesystem, and
receives only the existing database login and recovered-cluster CA.

## Planned staged rollout

1. After SAI-09 clearance, fetch the deployed source identities, integrate them
   into a clean release branch containing this commit, and record the current
   Helm revisions, image digests, CNPG cluster UID/generation, system node-group
   identity and Terraform state serials. Require the shared Helm releases to be
   terminal and healthy; a `pending-*` state is a no-go.
2. Change the retained private system-pool input from one to three regular
   nodes and set both node and backup capacity/cost acknowledgements only after
   reviewing live quota, current pricing and the private preflight receipt.
   Save the infrastructure binary plan and JSON. Require a no replacement
   result: the plan may add the dedicated bucket, identity/key and two system
   nodes, but it must not replace the cluster, public IP, database PVCs or any
   unrelated resource. Apply that exact reviewed plan and wait for all three
   system nodes to be Ready.
3. Plan and apply workloads with restore verification disabled. Confirm the
   database rolls one instance at a time and reaches three healthy instances on
   three different nodes. Confirm two Envoy replicas become Ready on distinct
   nodes and the PDB has one allowed disruption.
4. Wait for a completed scheduled `Backup` and a non-null
   `status.firstRecoverabilityPoint`. Record `lastArchivedWal` twice across a
   controlled non-sensitive marker write and require it to advance; confirm
   continuous WAL archive health from CNPG status/events without reading backup
   contents.
5. Run the marker-preparation apply for the exact completed Backup. Capture the
   payload-free marker A/B LSNs and emitted target time. Run the distinct
   recovery apply and require marker A present, marker B absent, a replay LSN at
   or after A, and all sensitive-table denial checks. Save only status/events
   and bounded verifier output, never table contents.
6. Run the marker-cleanup apply with the same exact identity. Require its
   payload-free success receipt, then clear every acceptance flag/identity and
   apply the saved resource-cleanup plan. Confirm the marker table/grant,
   marker Job, cleanup Job, recovery Cluster, verifier Job, PVC and Pods are
   absent; retain the production backups.
7. Under an approved one-system-node disruption, require both Envoy replicas to
   start on distinct nodes, the PDB to retain one available replica, and the
   public endpoint to remain healthy. Restore the node and require 2/2 Ready.
8. Re-run anonymous landing/catalog, lead submission, PAT/model grants,
   synchronous and streaming inference, MCP initialize/list/call, admin
   session/role, operations/results/artifacts/uploads, customer storage,
   Kueue/model admission, request-debug and observability smoke tests. Verify
   the sibling MindEval resources and all previously deployed application
   digests/features survived the rollout.

Example non-secret verification commands:

```bash
# Set KUBECONFIG to the private retained-cluster kubeconfig recorded in the task.
kubectl --context k8s-inference-h100 -n fs2-data get scheduledbackups,backups
kubectl --context k8s-inference-h100 -n fs2-data get cluster fs2-control-db \
  -o jsonpath='{.status.firstRecoverabilityPoint}{"\n"}'
kubectl --context k8s-inference-h100 -n fs2-data get cluster fs2-control-db \
  -o jsonpath='{.status.lastArchivedWal}{"\n"}'
kubectl --context k8s-inference-h100 -n fs2-data get pods \
  -l cnpg.io/cluster=fs2-control-db -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
kubectl --context k8s-inference-h100 -n envoy-gateway-system get pods \
  -l gateway.envoyproxy.io/owning-gateway-name=public \
  -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
kubectl --context k8s-inference-h100 -n envoy-gateway-system get pdb
kubectl --context k8s-inference-h100 -n fs2-data logs job/fs2-control-db-pitr-marker \
  | grep '^FS2_PITR_TARGET_TIME='
kubectl --context k8s-inference-h100 -n fs2-data get job fs2-control-db-restore-verifier
kubectl --context k8s-inference-h100 -n fs2-data logs job/fs2-control-db-pitr-marker-cleanup
```

## Rollback and retention

Rollback is source- and state-driven, not an ad-hoc `helm rollback`. Preserve
the pre-rollout saved plans, state serials, release revisions, image digests and
the last known-good source commit. If system-pool expansion succeeds but a
workload step fails, keep all three nodes while restoring the last known-good
workload source. If backup configuration fails, restore the prior CNPG spec
without deleting the new bucket, key or any objects. If the Envoy rollout
fails, restore the exact prior reconciled chart values while retaining the
expanded node pool. Never shrink the node pool, delete the retained bucket, or
discard WAL during incident rollback.

The temporary recovery cluster is not rollback data; completing marker cleanup,
then setting all three acceptance phases to false and clearing their identity
fields, removes temporary resources after evidence capture. The durable backup
bucket is rollback data and intentionally blocks an
ordinary full-stack destroy until an operator explicitly adopts it or follows a
separately reviewed data-retirement procedure. Do not roll back by shrinking
the system pool while the database or edge depends on three-node placement.

## Current verification and cost state

Wave 1 performed source tests and read-only live inspection only. It created no
cloud, Kubernetes, bucket, key, node, PVC, Pod, Job, backup or GPU resource and
therefore incurred no incremental cost and requires no cleanup. This is not a
live SAI-06 closure claim: successful backups, `firstRecoverabilityPoint`,
three-node PostgreSQL spread, two-node Envoy spread and recovered-cluster Job
success remain mandatory after the parent releases the SAI-09 gate. No model or
GPU behavior changes in this remediation, so a GPU verification run is not
applicable. The eventual plan adds two regular CPU system nodes and a retained
6,144 GiB object-storage ceiling by default. A read-only live check on
2026-09-16 observed 239,727,929,141 bytes of Standard-storage use in the target
project/region and no explicit ceiling in the returned allowance. This is
usage evidence, not capacity or price approval; refresh the receipt and record
the provider plan and current pricing before approving the no-replacement plan.

The source gate completed with these exact results:

- root, infrastructure and workloads `terraform validate`: pass;
- focused infrastructure backup tests: 3 passed, including retention-aware
  sizing acceptance and rejection of both a disposable bucket and 256 GiB;
- focused SAI-06, deployment-contract, wrapper and infrastructure-contract
  tests: 164 passed plus 104 parameterized subtests;
- Helm lint and public-edge rendering: pass, including exact Envoy replica,
  anti-affinity and PDB assertions; one-replica and disabled-PDB inputs were
  both rejected;
- Trivy HIGH/CRITICAL configuration scans: zero findings in the changed backup
  and database Terraform; repository secret scan: zero findings;
- Ruff lint over the changed Python wrapper/tests, Terraform recursive format
  check and `git diff --check`: pass.

The broader current checkout is not represented as green. The workloads
Terraform suite reported 37 passed, 3 failed and 10 skipped; all three new
PITR plan cases passed, while the failures remain in untouched
general-CPU/scientific-artifact cases. The broad Python suite reported 497
passed, 14 failed and 492 parameterized subtests passed; failures were outside
the SAI-06 diff (one missing optional host dependency, existing public-export
history, two scheduling-contract checks and ten scientific receipt-identity
mismatches). These negative results are retained rather than rewritten as
promotion evidence; the focused SAI-06 gate above is green.
