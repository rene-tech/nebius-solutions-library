# AlphaFold 3 reference-data live remediation evidence

Target: project `project-e00rene`, region `eu-north1`, Kubernetes context
`k8s-inference-h100`, namespace `fs2-reference-data`. Kubeconfig
`~/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig`.
No B300 resource was read or mutated.

## Preserved in-flight staging

Two stagers were running when this work started and both were preserved. No
pod, Job, ConfigMap, queue, filesystem object or Terraform resource was
deleted, patched or applied during the remediation.

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

`Deployment/fs2-reference-data-status` is 1/1 Running.

* `/healthz` returns `ok`.
* `/readyz` returns HTTP 503 `not ready`.
* `/v1/status` returns `{"ready": false, "ready_items": 0, "items": [],
  "invalid_items": 0, "scan_errors": 0}`.
* `/metrics` reports `fs2_reference_data_dataset_ready 0`.

The plane truthfully reports AlphaFold 3 as not ready. No dataset revision is
published, so no readiness claim is made.

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

Both live reference nodes are `8vcpu-32gb`, schedulable at 7,000 millicores and
28,672 MiB, behind a Kueue nominal quota of 6 CPU / 24Gi. That is enough to
download, decompress and hash the databases, and it is not enough to run
jackhmmer and nhmmer over them. `reference_data.py capacity-requirements`
reports the split directly:

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

The AlphaFold 3 data pipeline needs an AlphaFold 3 runtime image. The image
that produced the existing H100 semantic evidence is recorded as
`sha256:eaea560ce2ddba8d828371d1cba01da954d9a68ff5e77ba4d43b36b107141887`, but
its repository is deliberately `withheld` in `academic-assets/contracts`, it is
marked `role: historical-semantic-evidence` with `final_wrapper: false`, and
the `fs2-academic-poc` namespace holds no running workload or image pull secret
from which the concrete registry path could be read. The final AlphaFold 3
runtime wrapper is owned by
`fs2-cancer-images-structure-secondary-r20260902` and is not yet published.

**Runtime owner update, 2026-09-03.** The AlphaFold 3 semantic and runtime
contract passed, but the candidate digest `d8cf` is provenance-blocked and is
being rebuilt from a clean exact source. No raw Job may be started on `d8cf`;
the raw run waits for the next reviewed `r6` digest. That is a hold on the
runtime image only. It does not affect the reference-data tree, whose identity
is established independently by the manifest and terminal receipt.

That image also lays out its interpreter and entrypoint differently from the
path the executor previously hardcoded: it uses `/alphafold3_venv/bin/python3`
and `/app/alphafold/run_alphafold.py`, not `/opt/alphafold3/run_alphafold.py`.
The request contract now declares the interpreter and script explicitly, with
absolute-path and character validation, so the acceptance run binds whichever
layout the published wrapper actually uses instead of assuming one.

## Not yet done, and why

**The immutable tree is still being materialized.** The preserved stager began
expanding the bundle at 03:24Z. Decompressing 222.4 GiB of sources into roughly
630 GB and hashing every expanded file is a multi-hour operation, and it was
deliberately left to run rather than being restarted under the new tooling.
Until it publishes there is no manifest digest, no aggregate tree digest and no
terminal receipt, so none is quoted anywhere.

**The semantic raw-input run is blocked on an AlphaFold 3 runtime image.** The
data pipeline needs an image containing `run_alphafold.py`. No such image is
obtainable from this task: the registry path of the one that produced the
existing H100 evidence is deliberately withheld, that image is explicitly not
the runtime wrapper, the final wrapper owned by
`fs2-cancer-images-structure-secondary-r20260902` is unpublished, and neither
service account carries an image pull secret that would resolve a path. This is
a missing-resource dependency, not a defect in this work, and it is recorded
rather than worked around.

`scripts/validate_published_revision.py` validates a published revision in
place and writes nothing: the status document, the manifest and its digest, the
canonical dataset sub-path, the readiness marker, the read-only mode of the
tree, and the walked file count and byte total against the manifest. For a
revision published in the pre-bounded shape it also derives, without writing,
the exact terminal receipt the bounded contract requires. It is the first thing
to run the moment the tree publishes.

Once the reviewed runtime digest is available, the part of acceptance that does
not need it can still be run and is worth running: a rendered raw-input Job admitted into
`reference-data-cpu`, scheduled onto a reference-data node under its taint,
mounting the published tree read-only, validating the input object checksum,
the reference manifest digest, the `/sha256/<tree>` path component and the
`.fs2-manifest-sha256` marker against the real published values, and then
failing closed on the absent runtime executable. That exercises everything this
task owns and names the external dependency precisely.

No AlphaFold 3 readiness is claimed. The status service reports
`fs2_reference_data_dataset_ready 0` and `/readyz` returns 503, which is the
truthful state. Readiness requires the parameter identity, the reference
database tree and manifest identity, the runtime image digest, Job scheduling
and a semantic H100 run all to pass; four of those five are outstanding.

## Cleanup

The tooling copied into the stager's `emptyDir` scratch space for the read-only
plan runs was removed, and the hard-link probe directory was removed
immediately after the probe. The shared filesystem holds only the stager's own
`blobs`, `downloads`, `locks` and `.staging` directories. One shared
reference-data filesystem, no split copy, no destructive restaging.
