# Reference-data and private MSA plane

This directory implements versioned scientific reference data and CPU-only
sequence preprocessing behind the solution's root `deployment.storage.reference_data`
Terraform contract. Infrastructure creates a dedicated same-region filesystem,
private versioned bucket and least-privilege MysteryBox-backed S3 identity;
the workloads stage creates the private CPU/Kueue plane and can submit the
official pinned AlphaFold3 staging Job without a source-code edit.

The production default is private: customer sequence objects may be read only
from `file:///` or private `s3://` references, sequence content is never put in
Job metadata or logs. The dedicated `fs2-reference-data` namespace has
default-deny egress, and a tainted, storage-attached CPU pool keeps staging and
MSA work off both the Kubernetes system pool and H100 nodes. The root facade,
workloads contract, standalone Job renderer and module all reject `fs2-data`,
which is reserved for the live CloudNativePG cluster.
The root `cpu_pool.schedulable_capacity` contract is deliberately lower than the
provider preset's nominal capacity so Kubernetes and node DaemonSets retain
headroom. Root and module preconditions reject a pipeline pod that cannot fit one
worker, aggregate staging plus status requests that exceed the pool, or Kueue
quota above that conservative capacity. The production `8vcpu-32gb` example
declares 7000 millicores, 28 GiB memory and 112 GiB ephemeral storage as usable.
The separate public-MSA network lane requires both Terraform
`allow_public_msa_opt_in=true` and request/render-time opt-in. The included
backends perform local searches; the opt-in does not silently substitute a
public API.

See `AF3_REFERENCE_DATA.md` for the AlphaFold 3 path specifically: how staging
resumes rather than restarts, how objects are claimed individually so several
workers share the transfer, the bounded terminal handoff contract every consumer
binds, and how raw AlphaFold 3 input is placed on the CPU pool while inference is
placed independently by accelerator flavour.

Bulk database staging and the AlphaFold 3 raw data pipeline are sized
separately: the stager needs 6 CPU / 24 GiB and fits the dedicated
`8vcpu-32gb` reference pool, while the data pipeline needs at least
16 CPU / 64 GiB and does not. That requirement is recorded in
`model-requirements.json`, enforced against every request and at Terraform plan
time, and the pool's ability to admit the lane is reported rather than assumed.

The single public handoff is
`fs2-serve.nebius.ai/reference-data-terminal-receipt/v1`, defined by
`handoff-receipt.schema.json`, with a producer-generated consumer example at
`examples/af3-terminal-handoff.example.json`. Placement and sizing for the
reference and raw-input stages come only from root `terraform.tfvars`; the
reviewed default is `placement-contract.json`.

## Pinned inventory

`source-catalog.json` is the machine-readable authority. Sizes below are exact
compressed source-object byte sums; expanded size is not guessed when upstream
does not publish it.

| Bundle | Immutable upstream | Contents | Compressed | Expanded | Staging access |
|---|---|---|---:|---:|---|
| `alphafold3-public-databases-v3.0` | AlphaFold3 `231efc9bb9c13b45cc59e43f7107869084ee9624`; fetch script SHA-256 `152b5a1a…00d19` | BFD small, MGnify 2022-05, UniRef90 2022-05, UniProt 2021-04, PDB seqres/mmCIF 2022-09-28, RNAcentral, NT-RNA 2023-02-23, Rfam 14.9 | 238,784,443,666 B (222.385 GiB) | 630 GB upstream estimate | Public component sources; component terms retained in catalog |
| `colabfold-mmseqs-2025-08` | ColabFold `c35de0221f4d297a39edf4cf292ba2832e321edc`; setup script SHA-256 `ae89a23a…9112` | UniRef30 2302, environmental DB 202108, taxonomy 2025-08-04, PDB100 230517 sequence/Foldseek indexes | 256,499,341,805 B (238.884 GiB) | Not published | Non-secret terms receipt required because component redistribution terms need review |
| `protenix-v2-inference-data-2026-01-29` | Protenix v2 `2475421477ab414b571149ad4a875c390ff8a35d`; downloader SHA-256 `27da2585…dd89` | `common` CCD/RDKit data, search databases and mmCIF/templates | 119,425,157,782 B (111.223 GiB) | Not published | Non-secret terms receipt required because packaged data has mixed component terms |
| `boltzgen-inference-molecules-2026-08-11` | BoltzGen v0.3.2 `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`; HF dataset `c3d36fd276e9caf098c75d4113c6d5eb320b1a4c` | `mols.zip` inference molecule dictionary | 391,401,102 B (0.365 GiB) | Not published | Terms unresolved; receipt required and redistribution blocked pending review |

The total staged source input is 615,100,344,355 bytes (572.857 GiB), before
expansion. Each source also has an exact upstream MD5 or multipart ETag and byte
count. These are transport identities, not final trust anchors: staging always
computes SHA-256 for the downloaded blob and every expanded file before
publication.

Primary-source anchors are the
[AlphaFold3 v3.0.1 installation guide](https://github.com/google-deepmind/alphafold3/blob/v3.0.1/docs/installation.md),
[ColabFold pinned setup script](https://github.com/sokrypton/ColabFold/blob/c35de0221f4d297a39edf4cf292ba2832e321edc/setup_databases.sh),
[Protenix v2 data downloader](https://github.com/bytedance/Protenix/blob/2475421477ab414b571149ad4a875c390ff8a35d/scripts/database/download_protenix_data.sh),
[UniProt terms](https://www.uniprot.org/help/license/),
[Rfam terms](https://docs.rfam.org/en/latest/), and
[NCBI data policy](https://www.ncbi.nlm.nih.gov/%68ome/about/policies/).
Terms marked `upstream-terms-review-required` intentionally fail closed via an
access receipt. This inventory is an engineering access control, not legal
advice.

## Model compatibility

`model-requirements.json` maps AlphaFold3, OpenFold3 NIM, Protenix v2,
ESMFold2 Full/Fast, mosaic, BindCraft and RFdiffusion to the bundles and CPU
preprocessing contract they need.

- AlphaFold3 uses its frozen v3 database snapshot. Its default CCD pickle is a
  runtime-bundled artifact bound to the exact AlphaFold3 source/image revision;
  model parameters remain academic-gated and must never be redistributed.
- Protenix v2 obtains `components.cif` and its RDKit cache from the pinned
  `common` archive. AlphaFold3 and Protenix RNA/template files are shared only
  where the immutable source blob SHA-256 agrees.
- OpenFold3 NIM receives precomputed MSA/template request content and resolves
  CCD codes inside the entitled runtime; its NGC/NIM access is
  commercial-gated.
- ESMFold2 Full may consume private A3M. ESMFold2 Fast, BindCraft binder design,
  and RFdiffusion do not allocate an MSA database. BindCraft's separate
  PyRosetta dependency remains academic-gated.
- BoltzGen uses its pinned inference molecule dictionary; natural target MSA is
  optional and private, while designed chains remain MSA-free. Its dataset terms
  are explicitly unresolved, so staging fails closed without a receipt.
- Proteina-Complexa has no declared shared genetic/MSA database. Customer
  target/ligand objects are immutable inputs; NGC checkpoints and CCD/evaluation
  assets remain commercial-gated runtime artifacts owned by runtime onboarding.
- Mosaic inherits the selected backend's requirements. Its upstream public
  ColabFold defaults are not production-safe; admission should reject
  `use_msa=true` until the private adapter is integrated.

Frozen revisions are reviewed quarterly or when a pinned model release changes.
Promotion always creates a new revision; mutable `latest`/`current` aliases are
forbidden.

## Immutable storage contract

The internal module in `terraform/` consumes the exact storage and access
handoff emitted by `stages/infrastructure`. Its output describes this layout:

```text
s3://<bucket>/reference-data/blobs/sha256/<sha256>
s3://<bucket>/reference-data/manifests/sha256/<manifest-sha256>.json
file:///mnt/fs2-reference-data/data/
  datasets/<bundle>/<revision>/sha256/<tree-sha256>/
  manifests/sha256/<manifest-sha256>.json
  telemetry/
```

The shared filesystem is required because H100 workers have no local NVMe. A
GPU job should receive only the source input digest, preprocessing result
manifest digest, output URI and reference-data manifest digest. It mounts the
already-ready dataset read-only and must never run a reference download or MSA
search while holding a GPU.

`reference_data.py stage` uses a per-bundle filesystem lock, resumable `.part`
files, exact source identity checks, content-addressed blobs, safe archive
extraction, per-file SHA-256 inventory and atomic directory rename. A
`.fs2-manifest-sha256` marker is written only for a complete tree. Repeating the
same publication returns the original manifest and timestamp; it cannot replace
an immutable tree.

The default dedicated filesystem and object-store cap are both 2 TiB. Terraform
rejects less than 1,611 GiB: the official 630,000,000,000-byte AlphaFold3
expanded estimate plus 1 TiB of headroom, rounded up to GiB. Both sizes remain
tfvars parameters. The portable lifecycle defaults to `disposable` with
filesystem deletion protection disabled; retained storage requires an explicit
matched `retain` plus `forbid_deletion=true` opt-in. The bucket uses versioning,
forced STANDARD storage and a policy that limits the staging identity to
`reference-data/`, `inputs/` and `preprocessing/`.

## Operator workflow

Validate the checked-in catalog without downloading anything:

```bash
python3 reference-data/reference_data.py validate-catalog \
  --catalog reference-data/source-catalog.json
```

For a terms-gated bundle, hash the exact catalog `access.terms` canonical JSON
and create a non-secret receipt conforming to `access-receipt.schema.json`.
Receipts bind the bundle, revision, terms digest, approver, time and approved
scope; they contain neither download URLs nor credentials.

For normal operation, enable the root contract. Terraform provisions and
submits the suspended, Kueue-admitted CPU Job; Kueue unsuspends it only after
CPU/memory quota admission:

```hcl
storage = {
  reference_data = {
    enabled = true
    namespace = "fs2-reference-data"
    lifecycle = { retention_mode = "disposable" }
    cpu_pool = {
      platform        = "cpu-d3"
      preset          = "8vcpu-32gb"
      node_count      = 1
      schedulable_capacity = {
        cpu_millicores        = 7000
        memory_mib            = 28672
        ephemeral_storage_mib = 114688
      }
      boot_disk_type  = "NETWORK_SSD"
      boot_disk_gib   = 160
      max_surge       = 1
      max_unavailable = 0
      drain_timeout   = "15m"
    }
    filesystem    = { size_gib = 2048, forbid_deletion = false }
    object_storage = { max_size_gib = 2048 }
    network = {
      allow_public_source_staging = true
      allow_public_msa_opt_in      = false
    }
    status = {
      enabled = true
      image   = "cr.eu-north1.nebius.cloud/<registry>/<repo>@sha256:<digest>"
    }
    pipeline = {
      enabled   = true
      bundle_id = "alphafold3-public-databases-v3.0"
      image     = "cr.eu-north1.nebius.cloud/<registry>/<repo>@sha256:<digest>"
    }
  }
}
```

Accelerator attachment is independent of generic shared-cache attachment. Set
`accelerator_pools.<id>.reference_data_filesystem = true` only for pools whose
GPU Jobs consume an immutable reference tree. The default is false, so enabling
reference data cannot silently mutate future heterogeneous pools.

The portable default uses `retention_mode = "disposable"` with
`forbid_deletion = false`. It selects full reverse-order teardown across
workloads, foundation and infrastructure. Completion remains truthful: a
versioned bucket is deletable only while it has no current objects or retained
versions, and a provider teardown failure is never reported as success.

Retained storage remains available only through the explicit
`retention_mode = "retain"` with `forbid_deletion = true` opt-in. In that mode,
`inference-stack destroy` removes the workload and foundation states but keeps
the complete infrastructure state. It reports `infrastructure-retained` and
`full-stack-destroy-incomplete-infrastructure-retained`, then writes a
non-secret `reference-data-retention.json` adoption receipt containing the
filesystem, bucket, CPU-pool and last-applied status/pipeline identities. This
is a downstream-stage release, not a full stack destroy; full completion
remains blocked until the protected storage has been explicitly adopted or
migrated into another durable state.

`Dockerfile.stager` is the pinned CPU worker image. It contains only Python,
zstd, CA roots and the AWS CLI; the reviewed staging program and selected
single-bundle catalog are immutable ConfigMaps. The S3 secret is fetched from
MysteryBox as a Terraform ephemeral value and written with the Kubernetes
provider's write-only Secret field, so it appears in neither Terraform state nor
generated tfvars.

The renderer remains useful for an explicitly separate batch submission:

```bash
python3 reference-data/render_job.py stage \
  --catalog reference-data/source-catalog.json \
  --bundle colabfold-mmseqs-2025-08 \
  --access-receipt /secure/non-secret-colabfold-receipt.json \
  --image registry.example/stager@sha256:<64-hex-digest> \
  --object-store-prefix s3://<private-bucket>/reference-data \
  > /tmp/colabfold-stage.json
```

The generated Job is `suspend: true`, requests CPU/memory/ephemeral storage
only, and enters the dedicated Kueue LocalQueue. Source URLs represented by
`url_env` and credentials referenced by Secret keys are not copied into the
ConfigMap.

Validate and render private preprocessing:

```bash
python3 reference-data/reference_data.py validate-request \
  --request reference-data/examples/private-msa-request.json
python3 reference-data/render_job.py preprocess \
  --request reference-data/examples/private-msa-request.json \
  > /tmp/private-msa-job.json
```

The request contains object URIs plus SHA-256/size, never inline customer
sequence content. The worker verifies the input, publication manifest, mounted
tree marker and tree digest before running `colabfold_search`, Protenix input
preparation, or AlphaFold3's data-only phase. Completed filesystem outputs are
verified and returned as cache hits on retry. An immutable result manifest and
`.fs2-ready` digest are published with the output. Object-store results use both
the request SHA-256 and result-manifest SHA-256 in their path, and the readiness
marker uploads last; the target bucket must remain versioned.

## Readiness and telemetry

The optional status service has these read-only endpoints:

- `/healthz` — the HTTP service is running; Kubernetes readiness and liveness
  use only this endpoint, including on a fresh empty filesystem.
- `/readyz` — at least one published reference-data revision is ready.
- `/v1/status` — aggregate dataset readiness/counts plus revision,
  manifest/tree digests, bytes and file count. An empty filesystem is a healthy
  service with `ready: false` and an empty `items` list.
- `/v1/preprocessing` — content-free tenant/workload observations with backend,
  privacy mode, success/error, cache hit and latency.
- `/metrics` — an always-present aggregate dataset-readiness gauge, bounded
  per-bundle readiness/staging gauges and preprocessing observation, duration
  and cache-hit aggregates. Raw sequence content is never stored or labeled.

Status JSON and metrics expose exact revision readiness, staging duration/bytes,
content-free preprocessing latency/errors and cache hits. The official Job
retains `.part` files on the durable filesystem across retries, validates each
upstream size and MD5/ETag, computes a cryptographic SHA-256 for every source
blob and expanded file, uploads content-addressed blobs/manifests, and writes
the readiness marker last.

## Tests

Run the focused suite with:

```bash
reference-data/scripts/check.sh
```

It validates every JSON Schema, checks catalog byte totals, exercises atomic
idempotent staging and checksum/access failures, attacks archive paths, verifies
private-MSA/public-opt-in behavior and cache telemetry, renders a suspended
CPU-only Job, then formats and validates the integrated Terraform module.
