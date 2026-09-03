# AlphaFold 3 reference-data live remediation evidence

Target: project `project-e00rene`, region `eu-north1`, Kubernetes context
`k8s-inference-h100`, namespace `fs2-reference-data`. Kubeconfig
`~/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig`.
No B300 resource was read or mutated.

## Preserved in-flight staging

Two stagers were running when this work started and both were preserved. No
stager pod, Job, ConfigMap, queue, filesystem object or Terraform resource was
deleted, patched or replaced while staging was active. After the authoritative
staging Job completed, the successor applied only the reviewed bounded-upgrade
Job and immutable tools ConfigMap described below, then removed those two
temporary objects after recording their terminal evidence.

| pod | node | role |
| --- | --- | --- |
| `fs2-stage-af3-dcad60521606-q7b8w` | reference node A | orphan stager, actively downloading |
| `fs2-stage-af3-c0d769c79972-dtwvb` | reference node B | Terraform-owned stager, parked on the bundle lock |

The Terraform-owned stager was confirmed blocked rather than racing: its only
open file descriptor was
`/reference-data/locks/alphafold3-public-databases-v3.0.lock` and its thread was
sleeping in a filesystem lock wait. Cross-node `flock` on the shared
NETWORK_SSD filesystem is therefore honoured, and the two workers were never
writing the same partial file.

A second reference-data node joined during the work, and the previously Pending
Terraform Job scheduled onto it. Both reference nodes are 8 vCPU / 32 GiB,
tainted `workload.fs2.nebius/reference-data=true:NoSchedule`.

## Localization state, read-only, from inside the live pod

The new tooling was copied into the stager's own `emptyDir` scratch space
(digest `6f1130b771fa220f14bdda4eaa035f8f`, matching the repository file) and the
read-only `plan` command was run against the live shared filesystem. It stats
files only; it writes nothing.

```
python /work/reference_data_new.py plan \
  --catalog /etc/fs2-stage/catalog.json \
  --bundle alphafold3-public-databases-v3.0 \
  --root /reference-data
```

| object | state | source bytes | partial bytes |
| --- | --- | --- | --- |
| pdb-mmcif-2022-09-28 | adoptable | 56,979,074,571 | 0 |
| mgnify-2022-05 | adoptable | 69,333,008,025 | 0 |
| bfd-small | adoptable | 9,880,478,127 | 0 |
| uniref90-2022-05 | adoptable | 33,271,639,594 | 0 |
| uniprot-2021-04 | partial | 48,688,827,585 | 42,651,877,376 |
| pdb-seqres-2022-09-28 | missing | 26,493,534 | 0 |
| rnacentral-clustered | missing | 3,519,676,108 | 0 |
| nt-rna-2023-02-23 | missing | 17,028,641,580 | 0 |
| rfam-14.9-clustered | missing | 56,604,542 | 0 |

Totals at that observation: 169,464,200,317 adoptable bytes across four
objects, 42,651,877,376 partial bytes, 26,370,570,389 genuinely remaining, of
238,784,443,666 total. The four completed blobs carry no localization record
because they were produced by the pre-record staging code, which is exactly the
case adoption exists for: they are reused after digest re-verification rather
than re-downloaded.

`adoptable` is size-only evidence. Staging re-verifies the catalog transport
digest before adopting, so the plan is a plan, not a guarantee.

## The transfer completed without a restart

At 2026-09-03T03:26Z the same read-only plan reported every object present and
nothing left to fetch:

```
adoptable_objects  9
adoptable_bytes    238,784,443,666
partial_bytes      0
remaining_bytes    0
source_bytes       238,784,443,666
```

All nine AlphaFold 3 source objects, 238,784,443,666 bytes in total, were
localized by the preserved in-flight stager. Nothing was re-downloaded and no
byte of the transfer that was already on disk when this work began was lost.
Materialization into the immutable tree started at 03:24Z.

## Kueue scheduling

`ClusterQueue/reference-data-cpu` is `Active: True (Ready)` with nominal quota
6 CPU / 24Gi over `ResourceFlavor/reference-data-cpu`.
`LocalQueue/fs2-reference-data/reference-data` is `Active: True (Ready)`, "Can
submit new workloads". Three workloads are admitted and zero pending.

**Finding to hand to the Kueue scheduling owner.** Admitted workloads carry no
flavor assignment and no resource usage: `podSetAssignments` contains only
`{count: 1, name: "main"}` with no `flavors` or `resourceUsage`, and both
`flavorsUsage` and `flavorsReservation` report `total: 0` for cpu and memory.
Three workloads each requesting 6 CPU / 24Gi are therefore admitted against a
6 CPU / 24Gi nominal quota, so that quota is not currently constraining
admission. This is queue configuration, owned by
`fs2-cancer-immunotherapy-batch-priority-scheduling-r20260902`, not by the
reference-data plane.

This work does not depend on that being fixed. Both the Terraform precondition
and the render-time admission check reject an oversized request before a Job
exists, independent of whether Kueue enforces the quota.

## Status and readiness

`Deployment/fs2-reference-data-status` remained 1/1 Running throughout. Before
publication, `/readyz` returned HTTP 503, `/v1/status` contained zero items and
`fs2_reference_data_dataset_ready` was 0. The publisher did not expose an early
status while materializing the tree or copying the source objects.

After the bounded upgrade completed:

* `/healthz` returns `ok` and `/readyz` returns HTTP 200.
* `/v1/status` contains exactly one ready item, zero not-ready/invalid items and
  zero scan errors.
* `/metrics` reports `fs2_reference_data_dataset_ready 1`, one ready status
  item and zero not-ready/invalid items or scan errors.
* The ready item binds tree `d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea`,
  manifest `aa585259ce05393cd38db1693299ed9ec7f9c421aa4e1159f8d5aa0eb0ba9748`,
  inventory `38af3baa89a66cd24dec785279670a2e37597f98d206f555a04c138c6be71579`
  and receipt `b049e69846867caa75ef140e105a962fcf14e5c78ec8bfd97741cced32a8f6a6`.

This is reference-data readiness only. It is not a claim that an AlphaFold 3
platform route or raw-input workflow is runnable.

## Staging parallelism benchmark

`scripts/benchmark_parallel_staging.py`, eight incompressible 48 MiB objects,
comparing whole-bundle serialization against per-object claims. Both modes
publish one identical manifest digest across all workers.

| workers | serialized wall clock | per-object wall clock | speedup |
| --- | --- | --- | --- |
| 2 | 1.942 s | 1.128 s | 1.72x |
| 4 | 1.967 s | 0.779 s | 2.53x |

Two defects were found by running this benchmark and are fixed and regression
tested: adoption failed closed when two catalog objects shared a byte count,
and an idle poll timer dominated wall clock when every remaining object was
owned by a peer.

## Shared filesystem supports metadata-only assembly

Aggregate assembly links rather than copies, which is only sound if the shared
NETWORK_SSD filesystem supports hard links. Probed inside the stager against a
scratch directory that was removed afterwards:

```
{"cleaned_up": true, "hard_link_supported": true, "nlink": 2,
 "rename_supported": true, "same_inode": true}
```

Linking a file yields the same inode with a link count of 2, and renames work,
so promoting roughly 630 GB into the published tree is metadata work and never
a second copy. Where linking were unsupported the code falls back to renaming,
and a failure part-way through assembly loses only recomputable expansion work:
the downloaded blobs are untouched and a retry re-expands from them.

## Materialization, and why the handoff has to be bounded

The preserved stager expanded the PDB mmCIF snapshot first: a 56,979,074,571
byte archive decompressed to a 233 GiB intermediate tar, extracted, and the
intermediate released. At 05:03Z the staging tree held **195,859** mmCIF files
in 234.6 GiB, with the eight single-file sequence databases still to expand.

That number is the concrete reason the terminal handoff is bounded. The
published tree is roughly 195,867 files, about forty-eight times the
4,096-file inventory a consuming controller can safely validate. A manifest
carrying that inventory inline would be tens of megabytes and could not be
consumed. The bounded receipt instead carries the aggregate tree digest, an
independent manifest digest, the inventory digest and the counts, and the full
inventory lives in a separate content-addressed document that only a full audit
reads.

Filesystem use through materialization stayed well inside the 2.0 TB volume:
222.4 GiB of blobs, a 233 GiB peak for the intermediate archive, and 477.8 GiB
in use with 1.5 TB free once the archive was released.

## Terminal publication and bounded upgrade

The preserved orphan publisher completed without a restart and atomically
installed the read-only tree at 06:56:48Z. Its legacy manifest was
`a1b11b12eece39e6ea79f7c624611ed1b2664be73ab4882f744807f01dc32066`.
That 32,771,550-byte document carried all 195,867 file entries inline, so it
was a valid immutable publication but not the bounded consumer contract.

The Terraform-owned Job `fs2-stage-af3-c0d769c79972` was never restarted or
replaced. It acquired the original bundle lock after the orphan exited and
independently re-hashed the whole published tree. It completed successfully at
09:00:25Z and reported the same legacy manifest digest. Only after that
`Complete` condition did the successor apply the already-rendered upgrade:

| resource | identity | result |
| --- | --- | --- |
| accepted artifact | SHA-256 `082a051cf05b291048983f4475dff6664634f680f76a4d62d75994704f71bca6` | server-side dry run and apply passed |
| upgrade Job | `fs2-upgrade-af3-a3069616aae1`, UID `8d5c9b09-a48f-45a2-a64f-098664210539` | 09:01:24Z to 09:30:35Z, `Complete`, zero failed pods |
| Kueue Workload | `job-fs2-upgrade-af3-a3069616aae1-d3d34`, UID `064eb8d2-2a14-46c0-9e23-942f53695fba` | admitted by `reference-data-cpu` at 09:01:24Z |
| tools ConfigMap | `fs2-reference-data-upgrade-tools-a3069616aae1`, UID `c2a27076-9738-450f-9303-1f8066f5a8a1` | immutable; exact repository source and contract bytes |

The upgrade used the same immutable stager image digest
`sha256:89b826c4783bcb726f76fa55cbec4b9461ce774a0d8e11d7003c3a01e5eb5b44`,
6 CPU, 24 GiB memory and 2 GiB ephemeral storage. It selected only the stable
regular reference-data pool labels, tolerated only that pool's taint and
requested no accelerator. The embedded `reference_data.py`, requirements and
placement documents matched the task branch byte-for-byte at SHA-256
`d670ef1053dcbcb7cfa396d56360a9af5bc8279f7bc4e757d9d3effc1d6e2822`,
`88e1ab5cd6b029a39b857212d5b7e513e46389f09edbd088d6dbe956e72968ab`
and `e1036f7a760cf561be12643d85d40d8f02eff8f34193bd20a520938be70e39f4`.

Before writing metadata, the upgrade performed a second full content hash and
proved the exact aggregate identity again. It then published the external
inventory and bounded manifest to the private regional object prefix, changed
only the marker/manifest/inventory/receipt/status metadata, and did not
re-download or re-materialize a database byte. Its terminal values are:

| identity | exact value |
| --- | --- |
| tree SHA-256 | `d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea` |
| bounded manifest SHA-256 | `aa585259ce05393cd38db1693299ed9ec7f9c421aa4e1159f8d5aa0eb0ba9748` |
| inventory SHA-256 | `38af3baa89a66cd24dec785279670a2e37597f98d206f555a04c138c6be71579` |
| terminal receipt SHA-256 | `b049e69846867caa75ef140e105a962fcf14e5c78ec8bfd97741cced32a8f6a6` |
| files / expanded bytes | 195,867 / 672,435,030,513 |
| dataset sub-path | `datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/d27b8956170b5b0cf0f7daadf53a34e38cbe725dafbe9c91af86c671b32dfaea` |

The exact bounded document is checked in as
`evidence/af3-terminal-receipt-20260903.json`. Its canonical digest matches the
live `receipt_sha256`; the bounded manifest has zero inline inventory entries.

An independent read-only validation ran from the existing status pod after the
upgrade. It recomputed the canonical manifest, inventory and receipt digests,
walked and `lstat`-validated all 195,867 inventory paths, summed exactly
672,435,030,513 bytes, proved every entry is a regular non-symlink of its
recorded size, opened representative first/middle/last files, verified mode
`0555`, and bound `.fs2-manifest-sha256` to the new manifest. Every check
passed. This metadata/readability walk is independent of the two full content
hashes performed by the preserved staging Job and the upgrade Job.

## A sibling task shares this filesystem

At 05:47Z three directories appeared in `/reference-data/.staging` that this
task did not create: `fs2-boltzgen-archive-1`,
`fs2-boltzgen-boltzgen-checkpoints-1` and
`fs2-boltzgen-boltzgen-inference-molecules-1`, holding `mols.zip`,
`boltz2_aff.ckpt` and a molecule pickle, about 1.6 GB in total. They are
written by two pods in the `fs2-models` namespace,
`fs2-boltzgen-localize-checkpoints-r20260903` and
`fs2-boltzgen-localize-molecules-r20260903`, which mount the same host path
`/mnt/fs2-reference-data/data`.

This does not disturb AlphaFold 3 staging and the space is immaterial against
the 2.0 TB volume. It is recorded because it makes one property load-bearing:
**`.staging` is shared, so it must only ever be cleaned entry by entry.** The
publisher creates its working directories with `mkdtemp` under `.staging` and
removes only those, never the directory itself, and the tar path's intermediate
archive lives there too. A future change that cleaned `.staging` wholesale
would destroy a sibling task's in-flight artifacts, and equally a sibling that
cleaned it would destroy an in-flight reference-data expansion.

Two further observations for the program, not for this task: pods in
`fs2-models` are writing into the plane the reference-data module owns, and the
peak AlphaFold 3 footprint plus sibling artifacts share one volume. Neither is
a problem today; both are worth an explicit owner.

## Raw data-pipeline capacity is not satisfied by the reference pool

The bulk database stager and the AlphaFold 3 raw data pipeline are different
workloads and are sized separately.

| stage | declared sizing | admitted by the live reference pool |
| --- | --- | --- |
| bulk database staging | 6 CPU / 24 GiB | yes |
| AlphaFold 3 raw data pipeline | 16 CPU / 64 GiB | no |

Both live reference nodes are `8vcpu-32gb`; the terminal inventory reported
7,900 millicores and about 30.7 GiB allocatable on each, behind a Kueue nominal
quota of 6 CPU / 24Gi. That is enough to download, decompress and hash the
databases, and it is not enough to run jackhmmer and nhmmer over them.
`reference_data.py capacity-requirements` reports the split directly:

```
staging     pool=reference-cpu  requested={"cpu":"6","memory":"24Gi","ephemeral_storage":"2Gi"}   runnable=true
raw-input   pool=reference-cpu  requested={"cpu":"16","memory":"64Gi","ephemeral_storage":"32Gi"} runnable=false
inference   pool=accelerator    requested={"cpu":"12","memory":"64Gi","ephemeral_storage":"16Gi"} runnable=true
```

The requirement lives in `reference-data/model-requirements.json` under
`models.alphafold3.preprocessing_capacity`. A request declaring less is refused
outright, and Terraform rejects a `preprocess` block below it at plan time, so
the lane cannot be quietly shrunk to fit the pool that happens to exist.
Whether the pool can admit it is reported through the `raw_input_capacity`
output rather than failing the plan, so a fitting CPU class can be planned
before it is provisioned.

**This means the data-pipeline lane must not be advertised as runnable on the
current pool.** Provisioning that CPU class is scheduling and Terraform work,
and it is a precondition for any semantic raw-input run. As of 2026-09-03 the
general CPU task is adding a 16 CPU / 64 GiB capable reference class; when it
lands, `runnable_on_declared_pool` flips to true with no code change, because
the requirement and the pool contract are both data.

## Runtime dependency for the raw-input run

The final AlphaFold 3 academic runtime wrapper is published as tag
`3.0.4-85c4d205-r6`, immutable digest
`sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e`.
Its source/provenance and real H100 semantic evidence are accepted in the
runtime-owned contract. The concrete registry repository remains an explicit
deployment binding and is deliberately not committed; the digest is the
authoritative portable identity.

That image also lays out its interpreter and entrypoint differently from the
path the executor previously hardcoded: it uses `/alphafold3_venv/bin/python3`
and `/app/alphafold/run_alphafold.py`, not `/opt/alphafold3/run_alphafold.py`.
The request contract now declares the interpreter and script explicitly, with
absolute-path and character validation, so the acceptance run binds whichever
layout the published wrapper actually uses instead of assuming one.

## Remaining platform blockers

Reference publication is complete. A live raw-input or GPU run was not started
because the remaining dependencies are outside this publication task and the
ticket explicitly forbids GPU use before the adapter route is ready:

* Current main contains no AlphaFold 3 scientific workload profile or
  controller adapter registration. The reviewed adapter remains on its task
  branch and has not been integrated into the runnable platform route.
* The live reference pool remains 8 vCPU / 32 GiB per node and cannot admit the
  declared 16 CPU / 64 GiB AlphaFold 3 data pipeline. The separate general CPU
  pool task must be integrated and deployed; the request must not be silently
  reduced to fit the staging node.
* The runtime repository/image-pull deployment binding must be supplied by the
  runtime/controller integration. This task does not infer it from private
  credentials or bypass the withheld-repository contract.

These blockers do not weaken the reference-data result. The parameters and r6
runtime have their own accepted identities, while this task establishes the
database tree, manifest, inventory, terminal receipt, CPU Job scheduling and
node readability. Full AlphaFold 3 readiness still requires the integrated raw
route and a semantic H100 run using all of those exact identities together.

`scripts/validate_published_revision.py` validates a published revision in
place and writes nothing: the status document, the manifest and its digest, the
canonical dataset sub-path, the readiness marker, the read-only mode of the
tree, and the walked file count and byte total against the manifest. For a
revision published in the pre-bounded shape it also derives, without writing,
the exact terminal receipt the bounded contract requires. Its contract tests
cover both legacy and bounded publications plus tampered-tree rejection.

## Cleanup

The tooling copied into the stager's `emptyDir` scratch space for the read-only
plan runs was removed, and the hard-link probe directory was removed
immediately after the probe. After terminal validation, the task-owned upgrade
Job and immutable tools ConfigMap were deleted; their owner-controlled Kueue
Workload and pod were garbage-collected with the Job. `/readyz` remained HTTP
200 after cleanup. The published tree, manifests, inventory, receipt, status
service, original staging Jobs/pods, sibling artifacts and shared filesystem
were retained. One shared reference-data filesystem, no split copy, no
destructive restaging, no GPU allocation and no B300 access or mutation.
