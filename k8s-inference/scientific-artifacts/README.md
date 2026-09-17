# Scientific result artifact store

A dedicated, same-region, Terraform-managed object store for the results the
staged scientific batch controller commits. It is deliberately not the
reference-data bucket: that one holds immutable public science inputs that are
expensive to rebuild, this one holds tenant result bytes with a different
retention window and a different blast radius. Neither store's bucket, policy
or key is ever widened to serve the other.

The store module remains independently deployable. Enabling it creates the
bucket and one exact-prefix provider identity plus retained access-key
generations per explicitly declared tenant. It does not require, or enable,
staged batch execution or academic execution. The former shared-key identity,
group, access key and Secret remain declared with `prevent_destroy`. During the
mandatory reversible `legacy-overlap` phase its existing prefix grant is
retained so infrastructure-first rollout cannot strand the running gateway.
Ordinary applies cannot close that overlap. They also require every retained
credential generation to remain authorized and generation 1 to remain active.
Later generations may be prepared, but selecting one is rejected until a
future contract verifies independently witnessed readiness for its exact
provider, prefix, database and issuer path.

## What Terraform creates

`stages/infrastructure/scientific_artifacts.tf`, gated on
`deployment.storage.scientific_artifacts.enabled`:

| Resource | Purpose |
| --- | --- |
| `nebius_storage_v1_bucket` | Versioned, capacity-bounded, standard-class bucket in the cluster region |
| per-tenant broker workload | Performs authorized provider operations and mounts only one tenant's active generation |
| per-tenant service account and group | Provider principal for exactly one tenant and retained generation |
| bucket policy rule per tenant | delete-free `storage.uploader` + `storage.object-viewer` + `storage.object-lister` on `scientific/v1/tenants/<tenant>/*` |
| per-tenant access-key generation | Immutable Secret mounted only by the matching tenant broker |

Retention is two mutually exclusive resources rather than one flag, because
Terraform's `prevent_destroy` takes a literal and not an expression. The
default disposable bucket is eligible for a separately supervised removal only
once every version and delete marker is absent. A full-stack destroy remains
blocked in either bucket mode because every per-tenant credential generation
and the quarantined legacy identity are protected and exported for explicit
state adoption. `retention_mode = "retain"` additionally protects the bucket.

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

No provider S3 key is mounted into or returned to the shared control plane.
The former in-process static tenant credential loader is hard-disabled before
it can read any credential document and is not selectable through Settings or
the CLI.

1. Infrastructure creates a separate provider identity, exact-prefix group and
   access-key generation per tenant. Retained generations are additive and
   protected from destruction; only the selected active group is authorized.
2. `inference-stack` refuses a missing or malformed binding and passes it to
   workloads through the ordinary private tfvars handoff.
3. Workloads creates one independent broker per tenant, a dedicated least-privilege
   database login, TokenReview permission, projected Kubernetes reviewer and
   provider identities, TLS/CA mounts, and a default-deny NetworkPolicy.
4. The gateway mounts only its audience-bound broker token and CA. It forwards
   the original PAT or scientific-workload capability separately; the broker
   independently resolves the durable artifact/upload row and operation owner.
5. The broker mounts only its tenant's active immutable credential Secret,
   repeats durable tenant/action/version authorization, performs the provider
   operation, and returns no credential. Upload and exact-version inspection
   remain distinct broker actions.

Finalization records the immutable provider version; downloads and reads pin
that version. Inline reads verify the complete exact version before releasing a
byte, while large downloads use a version-pinned handle without a preliminary
full GET. See
`../components/control-plane/docs/artifact-store-credential-rotation.md` for the
mandatory broker policy, rotation, version-backfill, object-lock, integration
and rollback gates.

The isolated orphan-cleanup CronJob also owns abandoned signed PUTs. After a
fixed one-hour grace period it appends a finalization fence, discovers at most
one write-once current version without reading bytes, and records an issuer-
signed exact-version quarantine receipt. It never deletes provider bytes.
Claims and receipts survive operation-row retention, and interrupted work
resumes without an unversioned delete. Provider absence is closed with a
provider write fence and signed receipt so a late PUT cannot appear after the
claim becomes terminal.

`egress_cidrs` accepts only exact host addresses, `/32` or `/128`. The control
plane needs to reach the object-storage endpoint itself, not a subnet, and a
wider entry would open the default-deny egress policy further than the store
requires.

## Storage lifecycle

Two rules, both enabled, neither of which expires artifact bytes:

| Rule | Effect |
| --- | --- |
| `abort-incomplete-multipart-uploads` | Aborts parts 1 day after initiation |
| `remove-expired-delete-markers` | Removes tombstones with no versions left |

No rule expires a noncurrent version. Together with versioning and the absence
of `DeleteObject` on broker credentials, this preserves the exact finalized
VersionId even if a stolen uploader overwrites the canonical key. Provider
retention deletion is deliberately dormant, and expired bytes plus metadata
remain retained, until a separately scoped exact-version deletion principal
and signed expiry authority are implemented and independently reviewed.
`retention_days` remains the intended application retention window; this
candidate does not claim that physical retirement is active.

## Chart seam

`artifact-store-contract.json` is the written-down seam between the Terraform
projection and the control-plane chart. The workloads stage emits canonical
`scientificArtifacts` and `scientificBatch` values, broker URL/CA/projected
identity settings, `networkPolicy.artifactStoreCidrs`, and the non-secret
provider-binding revision. It emits no artifact-store credential Secret. The
obsolete `artifactService` wiring is not revived.

The chart's own declarations for `scientificArtifacts` and `scientificBatch`
belong to the batch-controller workstream. Until they merge, Helm ignores the
projected values and the store is provisioned but unconsumed, which is the
intended independently-deployable state. `tests/test_scientific_artifact_store_wiring.py`
asserts agreement as soon as the chart declares them.

## Checks

```
scientific-artifacts/run_checks.sh
```

Runs the layout unit tests, the Terraform-to-chart wiring tests, `terraform fmt`
and `validate` for both stages, and the `scientific_artifacts` Terraform test
files.

## Historical live evidence

The command below belongs to the rejected shared-key lineage and must not be
used to validate or promote the broker design:

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

That rejected historical smoke used `storage.object-editor` and destructive
cleanup. It is preserved only as negative evidence and must not be used to
describe or validate the delete-free broker policy in this candidate.

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
