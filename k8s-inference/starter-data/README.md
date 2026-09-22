# Customer workspace starter data

This directory builds immutable, versioned `examples/vN/` packs for customer
workspace buckets. Binary assets are release artifacts, not files in Git. Pack
Version 2 is model-example-qualified and deployed to all 12 currently eligible
distinct workspace/demo buckets; version 1 remains immutable and recoverable.
New eligible workspaces receive the selected qualified pack automatically.
Do not enable customer backfill with a draft pack. Exact release pins are in
[`release-v1.json`](release-v1.json) and
[`release-v2.json`](release-v2.json).

## Ownership and scope

The existing customer-storage service still owns bucket provisioning and scoped
credentials. `StarterPackService` is a separate background task, outside both
inference admission and provisioning. It enumerates authoritative workspace
records, uses their existing bucket-scoped credentials, and never enumerates all
cloud buckets. Shared buckets are deduplicated; private user buckets are distinct.
Disabled users, disabled credentials and excluded/disabled tenant storage stay
disabled. No quota or IAM expansion is part of this feature.

The installer uses conditional `PutObject(IfNoneMatch="*")` for every write,
including the completion manifest. Existing equal objects are checksum-verified
and adopted. Customer edits or conflicting manifests are preserved and reported.
Completion is recorded in PostgreSQL only after all bytes are verified. A
completed version is not installed again, even if the customer deletes examples.
Retries are bounded to eight attempts with at least five minutes between attempts.
One cross-replica advisory lock bounds installation concurrency to one bucket.

The loader caps a pack at 128 MiB, individual objects at 32 MiB, and object count
at 4,096. Bucket quota headroom is checked before uploading; concurrent customer
writes remain subject to provider quota enforcement.

## Content and provenance

Version 1 contains ten distinct cases in each of eleven live catalog categories,
covering 35 model IDs with 123 typed recipes. Data sources are:

- RCSB protein structures (CC0), reduced to one protein chain for compact inputs.
- PneumoniaMNIST teaching X-rays (CC BY 4.0), not clinical validation images.
- PriMock57 English role-play consultations (CC BY 4.0), including original human
  transcript references and clinician-authored notes.
- HHU German teaching consultations (CC BY 3.0 DE), including narration; there
  is no verified human German transcript in this pack.
- AltumAge published feature identifiers and synthetic preprocessing-center
  perturbations (MIT), never patient-level methylation data.
- Original synthetic count matrices, volumes, microscopy spots, laboratory
  profiles, prompts, video clips and LeRobot telemetry (Apache-2.0).

The version 2 microscopy extension adds ten real BBBC039v1 Hoechst-fluorescence
fields of cultured human U2OS osteosarcoma-cell nuclei. It deliberately spans
the official training, validation and test partitions and a 6–161 annotated
nuclei density range. Every case includes a contrast-normalized model input, the
official annotation decoded as a 16-bit instance-label PNG, the reference count
and per-instance pixel areas. Cellpose is the complete counting/segmentation
path; SAM 2 demonstrates automatic separation but its published request is
capped at 128 proposals, so it is not an exact counter for the densest fields.
BBBC039v1 is CC0 and is a research microscopy dataset, not patient tissue or
clinical-validation ground truth.

Each object has source, license, attribution, transformation, SHA-256, media type,
byte count, recipe version, compatible model IDs and validation status in the
manifest. Every example is for research/onboarding; no generated result is
presented as experimentally or clinically validated ground truth. Model access
remains governed by the user's existing API-key grants and model licenses.

`source-lock.json` pins public downloads. Full approved consultation WAVs are
losslessly encoded to FLAC and decoded back to PCM for byte-identical sample
verification; no speech is cut to fit an upload limit. The full pack is about
45 MB in qualified v2. Use the source manifest for exact release size.

## Build and qualification

`build_pack.py` requires pinned NumPy, Pillow, SciPy, Biopython, NiBabel and
AnnData, plus FFmpeg. Its arguments name the output directory, source cache,
captured live contracts, approved medical demo assets and synthetic AltumAge
fixture. It refuses an existing output directory and always writes
`release_status: draft`.

The reviewed `altumage-center-v2.json` build input is reconstructed from the
published 20,318 ordered CpG identifiers and preprocessing-center beta vector;
its checksum is pinned in `source-lock.json`. The fixture contains no person or
patient data.
The LeRobot fixture is generated with the repository's locked LeRobot v3 reader.

For additional models or corrected assets, build a new immutable version with
`--version v2` (and so on), capture current contracts, add meaningful cases and
rerun the affected public model recipes. `qualify_pack.py` requires a matching
semantic proof for every advertised case/model/input. Never replace a published
v1 image/manifest or clear completion rows to force an overwrite. The runner is
a POSIX Python 3.13 client; Windows users can use WSL or the hosted workbench.

`run_example.py` is copied into the pack as `run-example.py`. It resolves files
locally, validates the exact published typed schema, uploads immutable artifacts,
submits with durable idempotency, polls the original operation and verifies every
downloaded artifact. Reuse the output directory to resume. A changed recipe or
input cannot silently reuse an old run. It waits on explicit concurrency
backpressure; it does not raise customer limits. Ambiguous admission is retained
for reconciliation, never retried with a fresh idempotency key.

`validate_results.py` checks downloaded results against the exact pack used for a
cohort. It parses molecules, coordinates, label volumes, count matrices, images,
audio and model result contracts. HTTP/MCP acceptance is not semantic success.
Receipt files contain identities, checksums, timings and bounded checks, not
credentials or clinical input bodies. Qualification does not establish diagnostic
accuracy, affinity or therapeutic efficacy.

## Deployment and rollback

Terraform: `deployment.storage.customer_buckets.starter_pack` selects `enabled`,
an OCI `image` pinned by digest, and `manifest_sha256`. Helm maps these to
`customerStorage.starterPack`. The data-only image supplies `/pack`; a bounded
init container copies it to the gateway's read-only runtime mount. Model pods,
GPU controllers and database migration pods do not receive the pack volume.

The storage API exposes `examples` separately from bucket readiness. A failed
seed never disables usable storage. Disabling the seeder stops future writes;
rollback must retain installed examples, previous versions, customer data and
the completion ledger. No uninstall/delete behavior is provided.

Live qualification, exact image identities and backfill receipts belong in
`acceptance/customer-starter-data-20260920/` and
`acceptance/customer-starter-data-microscopy-20260922/`.
