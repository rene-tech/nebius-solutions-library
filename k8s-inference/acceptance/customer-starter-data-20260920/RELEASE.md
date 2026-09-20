# Customer workspace starter examples — 20 September 2026

Workspace rollout and independent backfill verification are complete at Helm
revision 195. Final supplementary model checks and test-identity cleanup remain
in progress; the initial 123-recipe qualification is complete.

## Delivered content and scope

Version 1 contains 110 distinct cases in 11 catalog categories, 123 typed MCP
recipes and 35 live model IDs. The immutable pack is 42,961,055 bytes including
its manifest (374 objects), under 0.9% of a default 5 GB workspace. See
[the complete coverage table](COVERAGE.md) and
[payload-free model checks](model-example-qualification.json).

Every advertised recipe has completed a real model operation through the public
MCP endpoint with a task-owned customer key, and its downloaded output passed
the relevant structural checks. This qualifies these specific examples, not
clinical accuracy, molecular affinity, physical action alignment or general
platform performance. Generated results are not ground truth.

The scientific examples include sequences and coordinates, docking inputs,
bounded protein-design campaigns, molecules, synthetic genomics/count matrices,
teaching X-rays, synthetic volumes/microscopy, synthetic aging features/labs,
full acted English/German consultations, speech generation, general text/images,
and robotics image/video/LeRobot inputs. Two SAM 2 examples were added when that
model appeared in the live catalog during the task.

RCSB structures are CC0; PneumoniaMNIST images and PriMock57 English role-play
audio are CC BY 4.0; HHU German teaching audio is CC BY 3.0 DE. AltumAge feature
identifiers/preprocessing-center synthetic inputs retain MIT attribution.
Original synthetic examples use Apache-2.0. Each object has provenance,
transformation, media type, size and SHA-256 in the pack manifest. No customer
uploads or real-patient recordings were copied. German teaching audio includes
narration and does not have a verified human transcript in this pack.

## Source and immutable release

- Repository: `rene-tech/nebius-solutions-library`.
- Branch: `agent/fs2-customer-bucket-starter-data-r20260920`.
- Backend source: `58500e3deb4e7d0d09f197f860d1e964633219f7`.
- Data qualification source: `c75d34e2a921f1cbbdfcf564fb6504d3fe4fc9f6`.
- Backend index: `sha256:2e1f8f5c4b836bd70a0b495087245668492faf271cb793dfd02348149d0073a3`.
- Pack index: `sha256:43a295ebc7bf0dcb4476ca98e86ae73d9cab1250479b7ae6164d2d732320f22f`.
- Pack manifest: `5ff0f043aca9cfb3c70a13423effb770e2eef9b6e1abbc9609cf5e877ec31ebc`.

Both images were copied with their OCI attestations/SBOMs and destination digests
verified. Exact repositories are in
[`starter-data/release-v1.json`](../../starter-data/release-v1.json).
Private keys, Helm values and raw model outputs remain outside Git. Binary input
assets are in the pinned data image, not the source repository.

The live target is cluster `mk8scluster-e00j5z9te7x5dd9g6a` in project
`project-e00rene`, public endpoint `https://89.169.99.188`. Shared-service drift
was detected before deployment: SAM 2 source `f4d6b1201` was merged into this
branch before building, preserving the accepted sibling release. The dirty
canonical checkout and security-remediation worktrees were not modified.

## Storage behavior

The existing storage service still owns provisioning, quota policy and scoped
credentials. The separate asynchronous seeder enumerates authoritative workspace
records, deduplicates shared buckets, and uses only existing bucket-scoped keys.
It never enumerates all cloud buckets and never uses a project-wide S3 key for
data writes. Disabled users, disabled credentials and excluded/disabled tenant
storage remain excluded. Inference admission never waits for seeding.

Each write uses `IfNoneMatch="*"`; equal existing objects are checksum-adopted,
conflicts are preserved and reported, and the immutable manifest is written last.
Completion is durable in PostgreSQL migration 0033. Completed versions are never
reinstalled, including after a customer deletes an example. Partial attempts
resume create-only, with at most eight attempts and a five-minute retry delay.
One cross-replica lock bounds work to one bucket at a time. Pack/object/inventory
budgets and quota-headroom checks are explicit; no cloud quotas or customer
limits were increased.

`GET /v1/storage` and the existing admin user-storage endpoint expose `examples`
separately from `state: ready`. Operators choose the immutable pack through
`deployment.storage.customer_buckets.starter_pack` in Terraform; the example
tfvars file and starter-data README document the keys. New installations must
pin an accessible approved data image; another region/project may mirror it
without changing the pack-manifest checksum. This task used guarded Helm
upgrades, not a broad Terraform apply against unrelated existing infrastructure.

## Verification performed

- 209 selected backend/storage/real-PostgreSQL/chart/packaging tests passed;
  33 recently deployed SAM 2 boundary tests also passed.
- A further 37 installer/service/runner tests and seven publication-coverage
  tests passed after client fixes. These counts overlap; do not sum them.
- Root and workloads Terraform validate; Helm rendering requires pinned image
  and manifest identities and isolates the data mount from GPU workloads.
- 123/123 recipes passed offline schemas and referenced-manifest role checks.
- 123/123 initial recipes have exact recipe/input-hash-bound semantic proofs.
  The qualification publisher refuses incomplete/model-mismatched coverage,
  altered inputs and missing provenance.
- Existing shared and private workspaces each seeded 374 objects on attempt 1.
  A new private user created after activation also received a distinct seeded
  bucket on attempt 1. Downloads were independently checksum-verified using
  customer API-key-disclosed scoped S3 credentials, not a project credential.
- The exact downloaded runner was installed in a fresh Python 3.13 environment
  from the bundled requirements and used for public OpenFold2, Qwen and SAM 2
  model calls. Two consecutive customer-shaped sample/download cohorts passed.
  Two additional cohorts on the final revision 195 each passed Qwen and SAM 2
  result checks using that same downloaded client and customer credentials.
- The task's shared-bucket README was backed up and deleted intentionally.
  Completion stayed recorded and the deleted object stayed absent across
  reconciliation cycles. No customer object was deleted.
- LeRobot output was independently reopened in the pinned 0.6.1 reader:
  16 decoded frames, all 208 non-video values identical, all selected frames
  changed. This does not establish physical alignment with recorded actions.

## Backfill outcome

All 15 eligible buckets completed on attempt 1: 12 existing non-task workspaces
and three controlled acceptance workspaces. Every installed object in all 12
non-task buckets was independently downloaded and checksum-verified with the
existing workspace-scoped credential. The three acceptance buckets have separate
full-download receipts. Shared users resolve to the same seeded bucket; private
users resolve to different seeded buckets. All 22 previously disabled storage
users, including Stockholm, remained disabled.

See [the payload-free deployment/backfill receipt](deployment-backfill-receipt.json).
Actual existing bucket names are retained only in the protected operator receipt
`/home/tux/secure-handoff/fs2-starter-backfill-final-20260920.json`; the published
receipt uses hashed bucket identities. No customer data outside the pinned
example prefix was downloaded or modified.

## Failures and corrections retained

Initial full WAV inputs exceeded the gateway's inline upload bound. Three own
empty reservations were cancelled, and full recordings were losslessly encoded
to FLAC; decoded samples match the originals. The runner now cancels its own
oversized reservation and never increases upload/concurrency limits.

ESMFold2-Fast and uploaded LeRobot initially used wrong referenced-manifest role
names despite passing the outer tool schema. The generator and pre-upload role
validator were corrected, then the same intended examples completed. Ambiguous
admissions are not blindly resubmitted with new idempotency keys.

Early CXR/Qwen completion budgets or prompting produced truncated/unusable
answers. Inputs were retained; documented image-first CXR prompting and sufficient
completion budgets were tested across every affected example. A 503 during a
read interrupted an early client run; its existing operation was resumed. The
runner now retries bounded, side-effect-free reads. Runtime provenance also
contains logical artifact names, not only public UUIDs; these now must match a
checksum-verified published output rather than causing an invalid-UUID failure.

These failures remain in the private task receipts. Later recipe-hash mismatches
in aggregate reports identify superseded drafts, not new model failures.
Existing Starlette test deprecation warnings were unrelated to this feature.
Qwen emitted a documented first-inference Triton compilation latency warning;
the cold request completed. No cold-start speed guarantee is made by this pack.

## Rollback and recovery

Helm revision 193 installed the backend with seeding disabled. Revision 194
enabled only the two acceptance tenants. Revision 195 cleared that allowlist,
enabling all eligible existing and future workspaces while preserving all other
live values. All pre-upgrade values
and manifests are retained privately; plans print only hashes and resource names.

To stop future seeding, set `customerStorage.starterPack.enabled=false` (or the
equivalent Terraform field) and perform the normal guarded rollout. Do not delete
installed prefixes, previous pack versions or the completion ledger. Rollback
to the preceding Helm revision also preserves them. A corrected pack must use a
new version rather than overwrite a previously completed `examples/v1/`.

The only removed object is the task-owned `examples/v1/README.md` deletion probe;
it remains recoverable from the pinned image and the local task backup.
