# AlphaFold 3 reference data and raw-input scheduling

This document describes how the AlphaFold 3 public reference databases are
staged onto the shared filesystem, what the published handoff contains, and how
raw AlphaFold 3 input is scheduled separately from GPU inference.

The AlphaFold 3 bundle is `alphafold3-public-databases-v3.0`, revision
`v3.0-paper-snapshot-2022-09-28`: nine public source objects totalling
238,784,443,666 compressed bytes, expanding to roughly 630 GB.

## 1. Staging resumes; it never restarts

Losing a partially completed multi-hundred-gigabyte transfer is the dominant
operational risk, so localization is content addressed and recorded.

* Each source object is downloaded to a stable per-object partial file and
  resumed with an HTTP range request when a worker restarts.
* Once verified against the catalog byte count and transport digest, the object
  is promoted to `blobs/sha256/<xx>/<sha256>` and a record is written to
  `sources/<bundle_id>/<revision>/<object_id>.json`.
* A later run reads that record and reuses the blob. It does not re-download and
  does not re-hash unless `--verify-existing-blobs` is given.
* An object that was already downloaded but has no record, which is what an
  interrupted pre-record run leaves behind, is **adopted**: a blob of exactly the
  catalog byte count is re-verified against the catalog transport digest and
  recorded. Only blobs this revision has not already claimed are candidates, so
  a bundle whose objects share a byte count does not re-hash every sibling.
* A record whose catalog identity no longer matches is a fail-closed error. A
  changed catalog requires a new immutable revision; it never silently refetches.

Inspect progress at any time without mutating anything:

```
python3 reference_data.py plan \
  --catalog /etc/fs2-stage/catalog.json \
  --bundle alphafold3-public-databases-v3.0 \
  --root /reference-data
```

It reports per object whether it is `localized`, `partial` or `missing`, the
partial byte count, and bundle totals including `remaining_bytes`.

## 2. Objects are claimed individually, not bundle-wide

Staging used to hold one exclusive lock for the whole bundle, so a second worker
on a second node sat idle for the entire run. Work is now claimed per object.

* Phase 1 holds the bundle lock **shared** and claims individual objects with
  non-blocking per-object locks under
  `locks/<bundle_id>/<revision>/<object_id>.lock`. Each worker downloads,
  verifies and decompresses the objects it wins, into its own content-addressed
  expansion under `expanded/<bundle_id>/<revision>/<object_id>/sha256/<blob>`.
* When every remaining object belongs to a peer, a worker blocks on that peer's
  object lock rather than sleeping on a timer, so it resumes the moment the
  object is published or the peer dies holding it.
* Phase 2 takes the bundle lock **exclusively** and does only the aggregate work:
  assemble the tree, compute the aggregate identity, publish the manifest,
  inventory and receipt, and atomically promote the tree.
* Assembly links rather than copies, so the aggregate promotion is metadata
  work, not a second copy of 630 GB. The per-object expansions are removed after
  publication because the published tree holds the same inodes.

A worker predating per-object locks holds the bundle lock exclusively for its
whole run. A new worker therefore probes that lock exclusively before entering
phase 1, so old and new workers are safely serialized rather than racing on the
same partial file.

Measured with `scripts/benchmark_parallel_staging.py` on eight incompressible
48 MiB objects, comparing whole-bundle serialization against per-object claims:

| workers | serialized | per-object | speedup |
| --- | --- | --- | --- |
| 2 | 1.942 s | 1.128 s | 1.72x |
| 4 | 1.967 s | 0.779 s | 2.53x |

Both modes publish a single identical manifest digest across all workers.

`.staging` is shared with other tasks that mount the same filesystem, so it is
only ever cleaned entry by entry. The publisher creates its working directories
with `mkdtemp` there and removes only its own; nothing removes `.staging`
itself.

## 3. The published handoff is bounded and content addressed

There is exactly one public handoff contract,
`fs2-serve.nebius.ai/reference-data-terminal-receipt/v1`, defined by
`handoff-receipt.schema.json` and published per revision at
`receipts/<bundle_id>/<revision>.json`.

A consumer cannot enumerate a reference database, so the receipt never contains
a file list. It carries:

* `storage.host_root`, the canonical shared filesystem root on the node.
* `storage.dataset_sub_path`, the canonical
  `datasets/<bundle>/<revision>/sha256/<64 hex>` path. Its `/sha256/` component
  is required to equal the aggregate tree digest exactly.
* `storage.mount_path` and `storage.read_only`.
* `content.tree_sha256`, the aggregate digest over the canonical
  path/bytes/sha256 inventory.
* `content.manifest_sha256`, an independent digest of the manifest document,
  required to differ from the tree digest.
* `content.inventory_sha256`, the digest of the separate inventory document, plus
  `content.file_count` and `content.expanded_bytes`.
* `content.inventory_marker`, the `.fs2-manifest-sha256` file inside the
  published tree whose content equals the manifest digest.
* `placement`, the CPU pool selector and tolerations. The data stage never
  declares an accelerator selector or an accelerator resource.

An inventory of at most 4096 files stays inline in the manifest; anything larger
is published only as the separate content-addressed inventory document and
referenced by digest. `content.inline_inventory` states which applies.

Field names are exact. A consumer draft that sends `published_manifest_sha256`
or `source_sub_path` is rejected with an error naming the actual field.

The publisher does not invent a manifest location. Deriving the four-key
`reference_data` block of a preprocess request is a consumer-side transform,
`derive_preprocess_reference_data(receipt, manifest_uri=...)`, which requires the
supplied URI to name the published manifest digest. `derive_database_root`
derives the mounted dataset path from the receipt alone.

## 4. Placement and sizing come only from root terraform.tfvars

`placement-contract.json` is the reviewed default; Terraform renders the CPU
pools and stages it owns into an immutable ConfigMap from
`storage.reference_data` in root `terraform.tfvars`, and the stager consumes it
at `/etc/fs2-placement/placement.json`. Any pool or stage the mounted document
declares replaces the reviewed default whole.

Nothing downstream carries a literal node name or accelerator generation. The
contract validator rejects `kubernetes.io/hostname`, any label key naming a
hardware generation, an accelerator selector or reservation on a CPU pool, and
an accelerator pool that routes work to the reference-data pool.

### The bulk stager and the data pipeline are sized separately

These are two different workloads and conflating them would advertise a lane
that cannot run.

| stage | sizing | runs on the current 8 vCPU / 32 GiB reference pool |
| --- | --- | --- |
| bulk database staging | 6 CPU / 24 GiB | yes |
| AlphaFold 3 raw data pipeline | at least 16 CPU / 64 GiB | no |

The bulk stager only downloads, decompresses and hashes, and 6 CPU / 24 GiB is
enough. The data pipeline runs jackhmmer and nhmmer over MGnify, UniRef90,
UniProt, BFD and the RNA databases; it is the CPU and memory bound stage of the
model. Its requirement is recorded in `model-requirements.json` under
`models.alphafold3.preprocessing_capacity` and is the single source of truth:

```hcl
# Root terraform.tfvars, storage.reference_data
pipeline   = { cpu = "6",  memory = "24Gi", ephemeral_storage = "2Gi" }
preprocess = { cpu = "16", memory = "64Gi", ephemeral_storage = "32Gi", threads = 16 }
```

Three separate checks keep this honest:

* A request declaring less than the model requirement is refused outright. That
  is a not-runnable request, not a slow one.
* Terraform rejects a `preprocess` block below the requirement at plan time, so
  the lane cannot be quietly shrunk to fit the pool that happens to exist.
* Whether the configured pool can actually admit the lane is **reported**, not
  enforced, by the `raw_input_capacity` output and by
  `reference_data.py capacity-requirements`. A fitting CPU class can therefore
  be planned before it exists, and until it does the lane truthfully reports
  `runnable: false` with the exact shortfall.

Rendering a Job still fails closed against the real pool, so an unschedulable
raw-input Job is never created: Kueue would leave it pending forever rather
than rejecting it.

To make the lane runnable, raise `cpu_pool.preset` and
`cpu_pool.schedulable_capacity` together with `queue.nominal_cpu` and
`queue.nominal_memory` to a class that satisfies the requirement. GPU inference
is placed independently through the accelerator pools and is unaffected.

```
python3 reference_data.py capacity-requirements
```

reports, per stage, the requested sizing, the pool it would use, whether it is
runnable, and why not.

## 5. Raw input and inference are separately placed stages

`render_job.py route --request <request>` resolves both stages of a raw-input
request and renders the CPU Job:

* **raw-input** is a CPU Job on the dedicated reference pool, carrying the
  reference-data node selector and its `NoSchedule` toleration, no accelerator,
  and sizing that fits the pool. AlphaFold 3 runs with `--norun_inference`, and
  the declared thread budget is passed as `--jackhmmer_n_cpu` and
  `--nhmmer_n_cpu` so the search tools do not oversubscribe the pool. A thread
  budget above the CPU request is rejected.
* **inference** is placed independently through the accelerator flavour:
  `accelerator.fs2.nebius/class` and `workload.fs2.nebius/gpu` with the
  `dedicated=fs2-inference` toleration and an explicit accelerator resource. It
  declares `needs: [raw-input]`, binds the handoff fields listed above, and
  records `reference_database_download: prohibited`.

### Consumer integration fixture

`examples/af3-terminal-handoff.example.json` is generated by the publisher's own
builder through `scripts/generate_handoff_example.py`, and a test asserts the
checked-in file still equals what that builder produces. A runtime or controller
integration test should load it rather than hand-writing a receipt, so it cannot
drift from what the publisher writes. Its digests are placeholders; the real
receipt is published to the shared filesystem, never to this repository.

`build_terminal_receipt(...)` is exported for tests that need a receipt with
different identities. Both it and the publisher validate through the same
function, so a fixture that would not validate cannot be produced.

The receipt deliberately does not carry `shared_filesystem_uri`. That field
belongs to the manifest, which is a publisher-internal document; a consumer
binds `storage.host_root`, `storage.mount_path` and `storage.dataset_sub_path`.

### Upgrading a publication made before this contract

A revision staged by an older worker carries its whole file inventory inside
the manifest and names no host root or dataset sub-path. Staging reports that
case by name rather than as a generic mismatch, and

```
python3 reference_data.py upgrade-publication \
  --catalog <catalog.json> --bundle <bundle-id> --root /reference-data
```

republishes it under the bounded contract. The existing immutable tree is
re-verified in place and left byte-for-byte unchanged; only the manifest,
inventory, in-tree marker, receipt and status are rewritten, and the original
publication timestamp is carried forward. Nothing is re-downloaded and nothing
is re-materialized. An upgrade whose tree no longer matches its recorded
aggregate identity is refused.

## 6. Operations

Validate the contracts:

```
python3 reference_data.py validate-placement
python3 reference_data.py validate-handoff --receipt <receipt.json>
python3 reference_data.py handoff --root /reference-data \
  --bundle alphafold3-public-databases-v3.0 \
  --revision v3.0-paper-snapshot-2022-09-28
```

`verify` checks the published tree. It is bounded by default and re-hashes the
whole tree only with `--verify-tree`.

Run the full module gate with `scripts/check.sh`.

## 7. Known limitations

* AlphaFold 3 upstream guidance sizes the data pipeline generously. The current
  dedicated pool is 8 vCPU / 32 GiB per node, which bounds the raw-input stage to
  6 CPU / 24 GiB. If a real run needs more, raise the pool through tfvars as
  described above rather than editing any rendered object; nothing in the code
  path assumes the present pool shape.
* The aggregate tree identity is computed by merging the per-object inventories,
  and the assembled tree is cross-checked path-by-path and size-by-size. A full
  re-hash of the assembled tree is behind `--verify-existing-blobs`, because
  re-hashing 630 GB on every publication is not a sensible default.
