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

Independent preliminary review then rejected successor
`ba2ff86cf78853726e2b5faf139f525b18bf6ec3`. That candidate preserved PITR
and privilege denial, but undercounted versioned non-current backup/WAL data,
accepted arbitrary cron cadences with daily sizing, lacked executable backup
and capacity alerts, allowed the default/null system pool to bypass cost
acknowledgement, and treated a provider response with no numeric limit as a
manual-review note rather than a hard stop. It is also immutable negative
evidence and is not rollout-authorized.

Independent final review then rejected
`d1a939e7bb90bb75e315c0e0097d32e0277f7a63`. Its HA, PITR, privilege-denial,
retention sizing and alerting remained useful, but its capacity JSON was
caller-authored and unsigned, provider compute/current-use enforcement was
incomplete, recurring applies double-counted existing bucket allocation,
destroy could remove workloads before retained-storage planning failed, and
the backup writer credential was reused by inventory and receipt workloads.
This successor preserves `d1a939e7` as NO-GO evidence; it is never an apply
input.

Independent source review then rejected
`71ecec4b27623e3d27ecbde63b18967d36bd90b7` / tree
`d4283ce38046495fb2f62eb12d99b8bb52be68f3`. The retained HA, marker-based
PITR, quota/delta, split monitoring/receipt identities and Envoy contracts were
positive, but rollout remained forbidden: trust could come from a dirty
worktree, same-size evidence mutation was not detected, Terraform reopened the
named plan after review, pure critical deletes were allowed, retained outputs
were only shape-checked, PITR reused the backup-writer key, inventory key
rotation did not roll the exporter, and the mandatory cost acknowledgements
broke unrelated CPU contract tests. This clean successor addresses those
findings without merging the independently unreviewed SAI-10 branch.

The final independent review rejected
`3d357a89f824940e4d7416cf332936278b19e43f` / tree
`bfa61465bc3cdb424da53c36f1ffdbb793e73b71`. Stable descriptor reads, the
separate read-only restore identity, quota delta accounting, HA/PITR and Envoy
semantics passed. Rollout remained NO-GO because infrastructure planning still
read a mutable checkout, the pure-delete guard enumerated only selected address
fragments, retained recovery checks trusted state-shaped truthy fields rather
than current provider and owned Kubernetes evidence, the exporter used a
collision-prone 24-bit rollout identity, and the broad repository checks were
not clean. That commit remains immutable negative evidence. This successor
addresses those findings additively and still does not integrate the rejected
SAI-08 or SAI-10 candidates.

The final independent review then rejected
`e3051f8d56cbeb18e397d9797703ff49cfaf86ff` / tree
`4bf2f45fbb344fa5df41bcd6a1b00eed62b4711f`. Its semantic plan-action
allowlist, sealed plan descriptor, provider-backed retained-bucket checks,
read-only restore identity, collision-resistant exporter rollout, quota delta,
HA/PITR and Envoy contracts passed. It remained source NO-GO because the root
configuration was planned before source identity was fixed and only the
infrastructure stage used immutable source, the recovery receipt was not bound
to the current Cluster/Backup/source/run, all-zero or non-advancing WAL plus an
almost-eight-day receipt could pass, and recovery evidence was not refreshed
after destroy planning. This successor corrects only those findings and keeps
`e3051f8` as immutable negative evidence.

The final independent review then rejected
`cfa4fed03f41f3fbd6e66165228bb22c5d3db194` / tree
`e23127dcd65637c41bbd381d02fe0a20c4bf3fc9`. Its complete immutable-source
snapshot, sealed-plan, semantic-action, bound-receipt and immediate
pre-action recovery checks materially fixed the preceding blockers. It
remained source NO-GO because the validator modeled generated Backups as
directly controlled by the Cluster even though the configured
`backupOwnerReference = "self"` makes the exact ScheduledBackup their owner,
and because it required a nonexistent `Cluster.status.lastArchivedWal` field.
This additive successor instead binds each Backup to the exact ScheduledBackup
UID and obtains WAL advancement from the documented `pg_stat_archiver` metrics
of the exact `Cluster.status.currentPrimary` pod. No synthetic Cluster status
field is accepted.
The contract follows CloudNativePG's official
[Backup owner-reference semantics](https://cloudnative-pg.io/docs/1.28/backup/#backup-owner-reference-specbackupownerreference),
[Cluster status API](https://cloudnative-pg.io/docs/devel/cloudnative-pg.v1/#clusterstatus),
and [predefined PostgreSQL metrics](https://cloudnative-pg.io/docs/1.28/monitoring/#predefined-set-of-metrics).

The final independent review then rejected
`7bfc479a99b3adb0991cb01a01d11124e24ffe67` / tree
`4af7ba98ac6006f16851f2ad29208cf8a0223e3c`. Its ScheduledBackup ownership
chain and preceding source, sealed-plan, recovery, provider, credential,
quota, HA, PITR, exporter and Envoy corrections passed, but its WAL proof
prepended the current Cluster timeline to CNPG's timeline-free rightmost-16-hex
LSN metric. A promotion could therefore turn a pre-promotion archive into a
fabricated post-promotion WAL identity. This successor preserves `7bfc479` as
negative evidence. It exports the complete `pg_stat_archiver.last_archived_wal`
and its paired archive timestamp through a primary-only, read-only custom CNPG
query; requires the full WAL timeline to equal the exact current timeline and
the archive timestamp to be strictly after `currentPrimaryTimestamp`; and
re-reads the Cluster to require the same UID, primary, primary timestamp and
timeline after collecting metrics.

## Non-destructive execution boundary

The task owner imposed a hard non-destructive constraint after the preceding
reviews. This source correction is therefore additive only. This task will not
execute apply, destroy, marker cleanup, node disruption, credential revocation,
resource replacement, cloud/cluster/registry/database mutation, or any test
that creates disposable state and later deletes it. It will not create live or
external verification resources. The rollout below remains a review plan, not
authority to execute it. Any acceptance step that requires deletion, cleanup,
disruption or replacement is blocked unless the owner changes that constraint.

## Implemented contract

The root facade always provisions a distinct versioned backup bucket. There is
intentionally no `enabled` input. The default recovery window is 30 days and
the only supported CloudNativePG schedule is the once-daily six-field cron
`0 0 2 * * *`. Other cadences fail validation rather than silently reusing
daily capacity math. The bucket has
`prevent_destroy`, keeps current backup/WAL objects under Barman retention, and
expires only incomplete uploads and non-current versions outside the recovery
window. Four distinct MysteryBox-delivered identities are mandatory:

- CloudNativePG alone receives `storage.object-editor` on
  `postgresql/v1/fs2-control-db/*` for base backup, WAL and Barman retention;
- the temporary recovery Cluster receives a separate read-only key with only
  `storage.object-lister` and `storage.object-viewer` on
  `postgresql/v1/fs2-control-db/*`; it cannot alter, delete or forge backups;
- the metrics validator receives only `storage.object-lister` and
  `storage.object-viewer` on `postgresql/v1/*` so it can inventory versions and
  content-validate a receipt but cannot upload or delete objects;
- the one-shot receipt publisher receives only `storage.uploader` on
  `postgresql/v1/restore-verification/success/*`; it cannot list, read or delete
  backup data.

Each secret half enters only its own Kubernetes Secret through Terraform
write-only data. The receipt-publisher Secret exists only for the bounded
restore-verification apply and is deleted when that flag is cleared; the
ordinary serving release cannot retrieve or use it. No one credential is
shared by CNPG, monitoring and receipt publication.

The role split follows the Nebius IAM role/action contracts: the object lister
can enumerate versions without object reads, the object viewer can read without
writes, and the uploader can put objects without list/read/delete privileges.
See the [Nebius Object Storage roles](https://docs.nebius.com/iam/authorization/roles)
and [supported S3 actions](https://docs.nebius.com/object-storage/supported-actions).

Backup capacity is retention-aware rather than a fixed 256 GiB. The enforced
minimum is:

```text
ceil(((database GiB * ((retention days + 2 current backups)
                     + (retention days + 7 non-current backup days)))
    + (estimated daily WAL GiB * ((retention days + 7 current WAL days)
                                + (retention days + 7 non-current WAL days))))
    * (100 + headroom percent) / 100)
```

For the full-catalog defaults this is 11,585 GiB: a 100 GiB database, one
daily base backup, 32 GiB/day estimated WAL, 30 retained days, seven additional
days before each deleted version expires, and 25% headroom. The default
retained bucket ceiling is 12,288 GiB. Both 256 GiB and the rejected 6,144 GiB
value fail validation before planning.

`inference-stack preflight` is observation-only. Before any root validation or
planning starts, the wrapper fixes a clean commit/tree identity, streams the
complete `k8s-inference` tree and its local `modules` from that
content-addressed Git object into a private read-only snapshot, and makes root,
infrastructure, foundation and workloads Terraform read only that snapshot. A
transient edit-and-revert of the mutable checkout therefore cannot affect any
stage plan. Source cleanliness and commit/tree identity are checked again
after the command. `plan` writes the exact
infrastructure binary plan and JSON, queries both `compute.instance.count` and
`storage.bucket.size.standard`, and emits a mode-0600 approval request with a
random 256-bit nonce. That request binds project, region, source commit and
tree, binary-plan and plan-JSON SHA-256, raw quota-evidence SHA-256, current
usage, numeric provider limits, existing and desired allocations, positive
incremental deltas, projected use, and the effective SAI-06 contract digest.

`apply` never replans infrastructure. It rejects any dirty, staged, submodule
or untracked source state, resolves the issuer registry from the exact reviewed
Git commit/tree blob, and securely snapshots the request and approval envelope
using directory-relative descriptors with `O_NOFOLLOW`. Each file read pins
owner, mode, link count, device, inode, size, mtime and ctime and compares two
complete reads before returning immutable bytes, so an in-place same-size
mutation is rejected. The binary Terraform plan is copied once into a Linux
write-sealed anonymous file. Both `terraform show -json` checks and the final
`terraform apply` use that same inherited descriptor; the named plan and stored
JSON are never reopened as authorization inputs. Infrastructure, foundation and
workload plans use a semantic action allowlist for every managed resource:
only `no-op`, `read`, `create`, or in-place `update` with a stable address is
accepted. Any delete, forget, move (`previous_address`), replacement,
malformed or future/unknown action fails closed; no address-fragment inventory
can omit a security group, egress rule, registry permit or future
outage-capable resource. It then verifies an
Ed25519 signature from an enabled committed issuer, a maximum 24-hour validity
window, the exact request/nonce, and both signed ceilings. The live provider
query is repeated immediately before apply; any usage, limit or evidence
change requires a new plan and signature.

Capacity uses the Terraform before/after values. A recurring no-op bucket
contributes zero bytes instead of adding the full 12 TiB allocation a second
time; system demand is likewise the positive node-count delta, while provider
projected usage remains current use plus that delta. A missing provider limit
fails closed unless the trusted issuer also has the `capacity-owner` role and
signs a resource-specific numeric override, reason and independent evidence
digest. An unsigned boolean or caller-selected public key is never authority.
The checked-in issuer registry intentionally contains no production key in
this source candidate, so shared apply remains blocked until an independently
reviewed owner key is added by the integration/release owner.

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
gate. Promotion requires an intentional change to three nodes and the
top-level `system_pool_cost_review_acknowledged` flag. That acknowledgement
applies to the effective profile-derived count even when `system_pool` is null;
the backup acknowledgement is independently mandatory. The trusted signed
capacity approval, no-replacement saved plan, quota and price review remain
additional gates. The remediation does not silently resize the retained
system.

Restore verification is deliberately a four-apply acceptance sequence after
one exact `Backup` has completed:

1. Enable `prepare_database_restore_marker` with the current source Cluster
   UID, that Backup's exact resource name, UID, completion time and nonzero
   end-WAL, plus a new non-sensitive marker ID. The bounded source Job
   commits marker A with its WAL LSN, emits a target timestamp, waits, and then
   commits marker B with its WAL LSN.
2. Disable marker preparation, enable `verify_database_restore`, and supply the
   captured target timestamp plus the currently archived WAL, which must be
   strictly later than the selected Backup's end-WAL. The temporary CNPG cluster recovers with
   `recoveryTarget.targetTime`. The verifier requires a non-null replay LSN,
   marker A at or before the replay boundary, and marker B absent. This proves
   archived WAL replay beyond the selected base backup and a bounded PITR stop.
   Only after that verifier succeeds, a separate no-service-account-token Job
   writes a non-sensitive, nonce-bearing v2 receipt under a content-addressed
   key in `postgresql/v1/restore-verification/success/`, using the upload-only
   publisher and an explicit S3 Signature Version 4 request. The receipt binds
   the publisher access-key identity, current source commit/tree, run ID,
   source Cluster UID, selected Backup name/UID/end-WAL and later verified WAL.
   Its validity is 24 hours. The read-only metrics identity fetches at
   most 8 KiB and validates the exact project, region, bucket, server, publisher,
   source commit/tree, run, Cluster UID, Backup name/UID/end-WAL, strictly
   advancing verified WAL,
   marker, target-time ordering, expiry, verification-subject digest and
   content-addressed object key before exporting its completion time. A forged,
   stale, oversized or differently scoped object is an exporter failure, not a
   restore success.
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

The release also creates a `ServiceMonitor` and `PrometheusRule`. Native CNPG
metrics alert on failed/stale backups, a missing first recoverability point,
WAL failures and an unarchived WAL backlog. A non-root, read-only exporter in
`fs2-data` inventories all current and non-current versions under the backup
prefix, reads only the newest bounded receipt for content validation, and
exposes total bytes, configured capacity, pressure, inventory health and the
newest validated restore-verification completion time plus bounded identity
labels needed by the live gate. It never exports object keys, payloads or
credentials. Warning/critical bucket thresholds are 80/90 percent, and restore
verification is stale after 36 hours. The live gate rejects a zero WAL, requires
the current archived WAL to advance beyond the selected completed Backup, and
requires the receipt's verified WAL to fall within that advancing range.
The exporter Pod template is annotated with the full 256-bit SHA-256 of the
inventory access-key ID, MysteryBox reference, provider resource version and
operator generation. Secret write-only revisions use a 60-bit prefix of that
same digest rather than the rejected 24-bit prefix; the reproduced resource
version 1872/2695 collision therefore cannot suppress a Secret update or a new
ReplicaSet. A MysteryBox key rotation cannot leave the old key resident.
The current release uses CloudNativePG's in-core `barmanObjectStore`, so its
native backup metrics remain populated even though CNPG has deprecated them in
favor of plugin-specific metrics. Any later Barman CNPG-I migration must change
the rules to the plugin metric names in the same reviewed rollout.

## Planned staged rollout

1. After SAI-09 clearance, fetch the deployed source identities, integrate them
   into a clean release branch containing this commit, and record the current
   Helm revisions, image digests, CNPG cluster UID/generation, system node-group
   identity and Terraform state serials. Require the shared Helm releases to be
   terminal and healthy; a `pending-*` state is a no-go.
2. Change the retained private system-pool input from one to three regular
   nodes and set both node and backup capacity/cost acknowledgements only after
   reviewing live quota and current pricing. Run `plan`, review its mode-0600
   approval request, and have a registered Ed25519 capacity issuer sign the
   exact request. If the provider again omits a numeric limit, require a
   `capacity-owner` signature over the exact numeric override and evidence
   digest. Pass only that envelope with `--sai06-capacity-receipt` to `apply`.
   Require a no replacement
   result: the plan may add the dedicated bucket, identity/key and two system
   nodes, but it must not replace the cluster, public IP, database PVCs or any
   unrelated resource. The wrapper rechecks plan hashes and live quota, then
   applies that exact reviewed plan and waits for all three
   system nodes to be Ready.
3. Plan and apply workloads with restore verification disabled. Confirm the
   database rolls one instance at a time and reaches three healthy instances on
   three different nodes. Confirm two Envoy replicas become Ready on distinct
   nodes and the PDB has one allowed disruption.
4. Wait for a completed scheduled `Backup` and a non-null
   `status.firstRecoverabilityPoint`. Require the generated Backup's controller
   owner to be the exact ScheduledBackup UID. From the exact current primary
   named by `Cluster.status.currentPrimary`, record the full
   `pg_stat_archiver.last_archived_wal` and paired archive timestamp exposed by
   the primary-only custom query across a controlled non-sensitive marker
   write. Require the full WAL's timeline to equal `Cluster.status.timelineID`,
   its archive time to be strictly after `currentPrimaryTimestamp`, and the WAL
   to be strictly beyond the selected Backup's `status.endWal`. Re-read the
   Cluster and require the same UID, current primary, primary timestamp and
   timeline before accepting the proof; never construct a WAL name from the
   predefined timeline-free LSN metric or read a fabricated Cluster field.
   Confirm continuous WAL archive health without reading backup contents.
   Query Prometheus for all SAI-06 rules and require their health; verify the
   bucket inventory reports current plus non-current bytes and is below the
   reviewed ceiling.
5. Run the marker-preparation apply for the exact completed Backup. Capture the
   payload-free marker A/B LSNs and emitted target time. Run the distinct
   recovery apply and require marker A present, marker B absent, a replay LSN at
   or after A, and all sensitive-table denial checks. Save only status/events
   and bounded verifier output, never table contents.
6. The marker-cleanup and temporary-resource removal steps are blocked by the
   current non-destructive constraint. Preserve the marker, recovery resources
   and receipt without executing cleanup until the owner supplies new explicit
   authority; do not claim final live acceptance while this gate is blocked.
7. The one-system-node disruption probe is likewise blocked by the current
   non-destructive constraint. When separately authorized, require both Envoy replicas to
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
  -o jsonpath='{.metadata.uid}{" "}{.status.firstRecoverabilityPoint}{" "}{.status.currentPrimary}{" "}{.status.currentPrimaryTimestamp}{" "}{.status.timelineID}{"\n"}'
PRIMARY=$(kubectl --context k8s-inference-h100 -n fs2-data get cluster fs2-control-db \
  -o jsonpath='{.status.currentPrimary}')
kubectl --context k8s-inference-h100 get --raw \
  "/api/v1/namespaces/fs2-data/pods/${PRIMARY}:9187/proxy/metrics" \
  | grep '^cnpg_\(pg_replication_in_recovery\|pg_stat_archiver_archived_count\|fs2_pg_stat_archiver_identity_identity\)'
kubectl --context k8s-inference-h100 -n fs2-data get pods \
  -l cnpg.io/cluster=fs2-control-db -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
kubectl --context k8s-inference-h100 -n envoy-gateway-system get pods \
  -l gateway.envoyproxy.io/owning-gateway-name=public \
  -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready
kubectl --context k8s-inference-h100 -n envoy-gateway-system get pdb
kubectl --context k8s-inference-h100 -n fs2-data logs job/fs2-control-db-pitr-marker \
  | grep '^FS2_PITR_TARGET_TIME='
kubectl --context k8s-inference-h100 -n fs2-data get job fs2-control-db-restore-verifier,fs2-control-db-restore-receipt
kubectl --context k8s-inference-h100 -n fs2-data logs job/fs2-control-db-pitr-marker-cleanup
kubectl --context k8s-inference-h100 -n fs2-data get prometheusrule,servicemonitor
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

`destroy` discovers that retained boundary before planning anything. It
requires the exact schema, project, region, bucket ID/name/capacity,
same-region endpoint, versioning, retention rules, destroy/adoption semantics,
four identity scopes, all 13 retained resource IDs and matching access-key
handoffs. It then performs a provider GET for that exact bucket and requires
its current parent project, region, name, capacity, active state, versioning,
storage class, anonymous-access posture and both lifecycle rules to match. The
current Kubernetes API objects must include a UUID cluster UID, the exact
active ScheduledBackup with its own UUID and `backupOwnerReference=self`, a
recent completed Backup controlled by that ScheduledBackup UID and naming the
same source Cluster, bounded real UTC timestamps, a recent first recoverability
point and healthy `ContinuousArchiving`. The exact current primary's real
primary-only `pg_stat_archiver` custom metric must report a recent full WAL
name on the stable current timeline, paired with an archive timestamp after
the current-primary timestamp and strictly later than that Backup's nonzero
`status.endWal`; the wrapper rechecks the same Cluster UID, primary, primary
timestamp and timeline after the scrape. Finally, the
read-only inventory exporter must have a fresh successful scrape, at least one
current object/version, internally consistent current plus non-current byte
counts, a matching bucket ceiling, safe remaining capacity and a fresh
content-validated restore receipt bound to that Cluster, Backup, source
commit/tree and run. After all downstream destroy plans exist, the wrapper
repeats the complete provider/Kubernetes/inventory/receipt check immediately
before the first action.
Only after those checks pass does it plan every eligible downstream destroy
stage. It applies none until all plans exist, always omits the retained
infrastructure stage, and writes a mode-0600 adoption receipt. Empty, stale,
cross-project or malformed outputs and an unhealthy backup/WAL boundary cause
zero plans and zero deletes. This provides a safe partial destroy instead of
discovering `prevent_destroy` only after an outage.

## Current verification and cost state

Wave 1 performed source tests and read-only live inspection only. It created no
cloud, Kubernetes, bucket, key, node, PVC, Pod, Job, backup or GPU resource and
therefore incurred no incremental cost and requires no cleanup. This is not a
live SAI-06 closure claim: successful backups, `firstRecoverabilityPoint`,
three-node PostgreSQL spread, two-node Envoy spread and recovered-cluster Job
success and all backup/WAL/bucket/restore alerts remain mandatory after the
parent releases the SAI-09 gate. No model or GPU behavior changes in this
remediation, so a GPU verification run is not applicable. The eventual plan
adds two regular CPU system nodes and a retained 12,288 GiB object-storage
ceiling by default. A read-only live check on 2026-09-16 observed
`compute.instance.count` usage 15 and `storage.bucket.size.standard` usage
239,792,496,897 bytes in the target project/region; both allowance records
omitted a numeric limit. This is usage evidence, not capacity or price
approval. The source now refuses apply unless fresh numeric provider limits
appear or an authenticated `capacity-owner` signs exact plan-bound numeric
overrides.

The following green results belong to the rejected `e3051f8` boundary and are
retained as prior evidence, not silently promoted to this successor:

- root, infrastructure and workloads `terraform validate`: pass;
- focused wrapper/SAI-06 plus deployment tests: 90 passed and 31 subtests;
  the focused SAI-06 file alone: 40 passed. The suite covers signed issuer
  verification,
  forgery/staleness/scope/insufficient-limit rejection, missing-limit owner
  override, committed trust, clean-source enforcement, exact-Git-object
  planning, same-inode mutation,
  sealed-plan path swapping/JSON divergence, exact request/resource validation,
  compute/storage delta math, replacement and pure-delete rejection,
  moved-address rejection, provider/object-version/owned-live recovery destroy
  gates, all-plans-first partial destroy,
  content-bound receipts, PITR, privilege denial, HA and alerts;
- the complete infrastructure Terraform suite passed 23/23. Its three focused
  backup tests cover the four exact bucket-policy identities plus
  retained/versioned sizing and negative lifecycle/capacity;
- Helm lint with explicit immutable image/catalog identities and HTTPS origins
  passed. Public-edge regression assertions cover exact Envoy replica,
  anti-affinity and PDB settings; one-replica and disabled-PDB inputs are both
  rejected;
- the exact pinned CloudNativePG/PostgreSQL image imported both `boto3 1.43.70`
  and the exporter under a read-only, network-disabled container run;
- strict Ruff passed for the wrapper, metrics implementation and SAI-06 tests;
  strict mypy passed for the metrics exporter with only boto3's missing type
  marker excluded; Python compilation, Terraform recursive format and
  `git diff --check` passed;
- Trivy HIGH/CRITICAL configuration scans found zero findings in the changed
  backup/database/monitoring Terraform, and the repository secret scan found
  zero secrets.

The complete workloads Terraform suite now passes 51/51. Its fixture repairs
bind the current immutable MSA CPU runtime digest, keep a chart-only academic
test out of the enabled queue path, explicitly acknowledge fair-share ordering
where an academic queue is tested, and isolate the scientific-artifact
same-bucket negative from an unrelated enabled reference-data CPU lane. No
admission or SAI-06 gate was weakened.

The broader repository is still not represented as wholly green: the root
Python suite reports 448 passed, 427 subtests passed, and 14 failures already
present at the rejected `3d357a89` boundary. They comprise the public-export
scan over thousands of retained private evidence references, two stale
source-string scheduling assertions, and ten pre-existing scientific
scheduler-receipt identity mismatches; running outside the control-plane
environment also adds a missing-Pydantic failure. These unrelated
evidence/provenance failures must not be rewritten in a database-resilience
branch. All changed SAI-06 paths and the complete workloads Terraform suite
are green.

The rejected SAI-08 successor and unaccepted SAI-10 successor `851a15df2`
remain deliberately unmerged and are not ancestors of this SAI-06 candidate.
The final independent review reported ten SAI-10 merge conflicts.
Reconciliation is forbidden until SAI-10 has an accepted clean successor; it
then belongs in a separate integration commit with both suites rerun, never in
either independently reviewed source lineage.

For this additive successor, the permitted local checks are deliberately
non-mutating: Python AST parsing of every changed Python executable/test,
`terraform fmt -check` for every changed HCL file, in-memory receipt/WAL/parser
probes, and `git diff --check`. Full pytest/Terraform/Helm suites create and
clean temporary files or provider working data and are therefore blocked by
the current no-delete/no-cleanup constraint. The full-WAL, promotion-time,
stable-Cluster and custom-query regression selection passes 14 tests with 39
deselected. Python compilation, Terraform formatting, diff whitespace, and
Ruff's fatal/syntax/import checks pass. Applying the control-plane Ruff profile
to the two complete legacy files reports 47 existing findings versus 49 at
`7bfc479` (23 unchanged in `inference-stack`, 24 rather than 26 in the focused
test); the successor adds no lint finding and removes the test import findings.
Neither legacy file is globally Ruff-formatted, so bulk formatting is outside
this narrow successor. No live verification or cleanup was attempted.
