# Scientific result artifact store

A dedicated, same-region, Terraform-managed object store for the results the
staged scientific batch controller commits. It is deliberately not the
reference-data bucket: that one holds immutable public science inputs that are
expensive to rebuild, this one holds tenant result bytes with a different
retention window and a different blast radius. Neither store's bucket, policy
or key is ever widened to serve the other.

The store is independently deployable. Enabling it creates a bucket and three
purpose-specific identities and keys and configures the control plane; it does
not require, and does not enable, staged batch execution or academic execution.

## What Terraform creates

`stages/infrastructure/scientific_artifacts.tf`, gated on
`deployment.storage.scientific_artifacts.enabled`:

| Resource | Purpose |
| --- | --- |
| `nebius_storage_v1_bucket` | Versioned, capacity-bounded, standard-class bucket in the cluster region |
| three `nebius_iam_v1_service_account` resources | Separate runtime writer, expiry remover, and independent absence verifier |
| three groups + memberships | Carry disjoint bucket-scoped grants |
| bucket policy rules | Writer/remover `storage.object-editor`; verifier `storage.object-viewer`; all on `scientific/v1/*` only |
| three `nebius_iam_v2_access_key` resources | Purpose-specific S3 keys, all `secret_delivery_mode = "MYSTERY_BOX"` |

Retention is two mutually exclusive resources rather than one flag, because
Terraform's `prevent_destroy` takes a literal and not an expression. The
default is the disposable bucket, which a supervised destroy removes once it is
empty. `retention_mode = "retain"` selects the protected bucket instead, which
blocks a full-stack destroy and exports its ID for explicit adoption.

## Object layout

```
scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>
    /shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>
```

Every component is a single path segment, so one tenant's prefix can never be a
prefix of another's: `scientific/v1/tenants/acme/` does not match a key under
`acme-labs`. The content digest is the last segment, so a retry that produces
identical bytes writes the identical key instead of forking a stage's committed
output. `artifact_store.py` owns the builder, the parser and those rules, and
`tests/test_artifact_store.py` pins them.

## Credential handling

The three S3 secrets never exist in Terraform state, a plan file, generated
tfvars, a Helm value, an output, a log or a receipt.

1. The infrastructure stage requests writer, remover, and verifier MysteryBox
   keys and exports only each access-key ID, opaque secret reference and
   revision.
2. `inference-stack` refuses a handoff that carries anything else and writes
   those three fields into the private workloads tfvars.
3. The workloads stage resolves each secret through an ephemeral MysteryBox
   entry and writes it with the Kubernetes provider's write-only argument into
   distinct `fs2-system` Secrets. Runtime receives only
   `fs2-serve-artifact-store`; the remover and verifier CronJobs receive only
   their corresponding store Secret.
4. The workloads stage derives the rollout identity as
   `credential_generation * 2^24` plus the first 24 bits of a digest over the
   key's non-secret identifiers. That value drives both `data_wo_revision` and
   the `fs2.nebius.ai/artifact-store-credential-revision` pod annotation, so a
   rotation rewrites the Secret and restarts the control plane. The annotations
   carry numbers and an access-key ID, never credential material.

SAI-21 amendment: the finalization recovery controller is a fourth, isolated
consumer. Infrastructure issues a fourth MysteryBox key for
`fs2-serve-artifact-finalizer-store`; workloads also project the distinct
`fs2-serve-database-artifact-finalizer` Secret for NOLOGIN group
`fs2_serve_artifact_finalizer`. The finalizer mounts neither runtime Secret. Its
database authority is recovery-only: it can claim an expired generation and
read or settle only that exact unexpired claim, without global `SELECT` over
tenant uploads, quota reservations, or finalization leases.

The cloud key's own `resource_version` cannot carry rotation on its own: a
replaced key starts again at zero, so a revision derived from it would repeat
the previous value and leave the stale secret mounted. Rotation therefore has
two independent triggers. Replacing the key changes its resource ID and its
access-key ID; `credential_generation` lets an operator force a rewrite without
touching the key. Write-only Secret data needs Terraform 1.11 or newer, which
the workloads stage now requires.

Workers never mount any of those Secrets. The control plane is the writer
credential's only consumer and hands workers short-lived signed handles bounded
by `handle_ttl_seconds`. The remover can delete but cannot release quota. The
read-only verifier re-fetches exact-key list and HEAD absence and alone can call
the database release routine.

`egress_cidrs` accepts only exact host addresses, `/32` or `/128`. The control
plane needs to reach the object-storage endpoint itself, not a subnet, and a
wider entry would open the default-deny egress policy further than the store
requires.

## Storage lifecycle

Three rules, all enabled, none of which touches a current object:

| Rule | Effect |
| --- | --- |
| `abort-incomplete-multipart-uploads` | Aborts parts 1 day after initiation |
| `expire-noncurrent-versions` | Expires superseded versions after the configured application-retention period |
| `remove-expired-delete-markers` | Removes tombstones with no versions left |

Deleting a live result is an application decision made against the durable
result record, which is why `retention_days` is passed to the control plane as
`retentionSeconds` rather than expressed as a bucket expiration.

## Chart seam

`artifact-store-contract.json` is the written-down seam between the Terraform
projection and the control-plane chart. The workloads stage emits canonical
`scientificArtifacts` and `scientificBatch` values, the three artifact Secret
references, separate maintenance schedules, `networkPolicy.artifactStoreCidrs`
and the rotation pod annotation. The obsolete `artifactService` wiring is not
revived.

The chart declares the exact `scientificArtifacts` and `scientificBatch`
projection, including tenant byte/object ceilings, upload completion/provider
stability grace, maintenance throughput/deadline bounds, and distinct runtime,
remover, verifier, and finalizer identities. The static wiring contract covers
those names so a Terraform/chart drift fails before rollout.

SAI-21 amendment: the chart seam now contains four artifact Secret references:
writer, remover, verifier, and finalizer. The finalizer reference is paired
with its own database Secret and recovery-only role; it is not an alias for the
runtime writer credential.

Customer begin-upload remains backward shaped: no protocol field means
`single-put-v1` and still returns a required handle. The handle now targets one
exact server-owned multipart part rather than replayable PutObject. Objects
above 5 GiB explicitly select `multipart-v2`; each subsequent part requires a
tenant-authorized length/checksum-bound handle, and only the control plane can
complete the session and pin the provider VersionId.

## Checks

```
scientific-artifacts/run_checks.sh
```

Runs the layout unit tests, the Terraform-to-chart wiring tests, `terraform fmt`
and `validate` for both stages, and the `scientific_artifacts` Terraform test
files.

## Live smoke test

Against a provisioned store, using the same credential document the Kubernetes
Secret carries:

```
python3 scientific-artifacts/artifact_store.py smoke \
  --endpoint https://storage.eu-north1.nebius.cloud \
  --bucket <bucket> --region eu-north1 \
  --credentials-file <path to credentials.json> \
  --operation <operation-id>
```

It signs an upload handle, uploads through it, finalizes by streaming the stored
object back and comparing the digest and size, signs a download handle, reads
through it, proves a request outside `scientific/v1/` is denied with the same
key, and removes the object versions it created. Deletion is then re-checked
rather than assumed: the current key, the exact written version and the
previously issued signed handle must each answer an exact 404. A 403 is not
accepted as proof, because that is precisely the answer a bucket-scoped writer
gets for a key it may not read, and a probe that was never taken cannot count
either. The credential is only ever
read from a file, so it cannot appear in a process listing or a shell history.

The writer and the separately credentialed finalization reconciler hold
`storage.object-editor`, which is object scoped and therefore
cannot list the bucket. That is deliberate, and it is why cleanup deletes the
exact versions the store reported rather than enumerating a prefix. Noncurrent
versions remain available for the same interval as application metadata so an
immutable VersionId pin cannot be invalidated before normal retention cleanup;
the delete-marker rule then reclaims empty tombstones.

The finalizer never shares the runtime key or runtime database login. Its
object-store key is scoped to `scientific/v1/*`, while its database role can
only claim expired finalization leases and settle the resulting unexpired
recovery generation. Runtime settlement is separately fenced to an unexpired
generation-1 lease.

## Evidence

`evidence/h100-deployment.json` records the live run against `project-e00rene` /
`k8s-inference-h100` in `eu-north1`: the bucket policy and lifecycle as the
cloud reports them, the exact-SHA wrapper plan action counts, the real key
rotation, and the cleanup proof. `evidence/h100-live-smoke.json` is the raw
smoke receipt. Neither contains credential material, and both are redacted of
cloud IDs and absolute paths; the unredacted originals plus the saved plan JSON
for each stage are retained in the run root.

The configuration and infrastructure layers plan clean at the recorded commit.
The foundation layer plans two replacements there, of the cluster-contract
ConfigMap and the Kueue admission-ready marker. Both are keyed on
`source_commit` by `stages/foundation`, which this task does not touch, so they
fire on any commit advance; the workloads layer cannot be planned until one of
those foundation applies has happened.
