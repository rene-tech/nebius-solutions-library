# Private academic asset ingestion

This directory implements the non-redistributing intake boundary for native
BindCraft/PyRosetta and AlphaFold 3. It contains contracts, validators, and an
optional PyRosetta-only private-layer build path, but no package, model
parameter, credential, signed URL, license acceptance, or model output.

No readiness claim is checked into Git. The 2026-09-02 inventory was completed
before acquisition, and the subsequently authorized source bytes live only in
owner-only state and the task-owned private cache. No organization-terms
acceptance has been recorded: the current bytes are quarantined and both native
models truthfully remain `MissingLicenseAcceptance`.
OpenFold3 and `open-binder` remain independently named alternatives. Neither
aliases nor satisfies the native `alphafold3` or `bindcraft` readiness gate.

## Pinned sources and terms

This is an engineering control summary, not legal advice. An authorized person
must review the linked source terms before each acquisition or use.

### PyRosetta for BindCraft

BindCraft tag `v.1.5.3` resolves to commit
`a234a8d3af9fe3d2724209aa91d930280b72048b`. Its
[pinned environment](https://github.com/martinpacesa/BindCraft/blob/a234a8d3af9fe3d2724209aa91d930280b72048b/environment.yml)
uses Python 3.10 and
`pyrosetta=2025.24+release.8e1e5e54f0=py310_0`. The official RosettaCommons
Linux conda index recorded:

- filename `pyrosetta-2025.24+release.8e1e5e54f0-py310_0.conda`;
- size `1,435,146,644` bytes;
- SHA-256 `d1170ee50d5f02c3a6c84a7d0035dc961c7737024836eb5da882abe6ca51afbb`.

The exact [PyRosetta non-commercial license](https://github.com/RosettaCommons/rosetta/blob/de92a3c0dea8a010d372a22025e3e50bd4e2f33f/LICENSE.PyRosetta.md)
has SHA-256
`41de5b13b7ddb64fab2dda1cb45581b9d4c1ee6be7bd3ad90f562288bcdcffa1`.
It defines eligible non-commercial users, permits internal non-commercial
copies and use, requires commercial users to obtain a separate UW license, and
directs other non-commercial users to obtain PyRosetta from the official
distribution so they accept the terms. The
[official download page](https://www.pyrosetta.org/downloads) documents the
RosettaCommons conda channel and archive procedure.

Consequently, this workflow rejects a public image. An organization-internal
PyRosetta private layer is allowed by platform policy only when an authorized
organization representative records that scope. An individual acceptance stays
user-bound and may only use private staged storage.

### AlphaFold 3 parameters

The source instructions are pinned to AlphaFold3 commit
`c0f97eda2f1f482fd94d3a38bece18c7069b4a5c`. The
[official README](https://github.com/google-deepmind/alphafold3/blob/c0f97eda2f1f482fd94d3a38bece18c7069b4a5c/README.md)
states that parameters may only be used if received directly from Google and
now identifies Google's direct `af3.bin.zst` object. The pinned object metadata
observed on 2026-09-02 is generation `1780568696389861`, size
`1,020,545,840`, CRC32C `0h6mjg==`, and last modification
`2026-06-04T10:24:56Z`.

The exact [model-parameter terms](https://github.com/google-deepmind/alphafold3/blob/c0f97eda2f1f482fd94d3a38bece18c7069b4a5c/WEIGHTS_TERMS_OF_USE.md)
and [prohibited-use policy](https://github.com/google-deepmind/alphafold3/blob/c0f97eda2f1f482fd94d3a38bece18c7069b4a5c/WEIGHTS_PROHIBITED_USE_POLICY.md)
have SHA-256 values
`41adf62ff5eabc58831c828793988537948663c139f8b87d8d413851b150b6e5`
and `4992aebdc29bc7a9260bed6373bad05bb5a7e1b783451cd170163dd259f46c45`.
The terms limit use to qualifying non-commercial organizations and research,
prohibit publishing or sharing model parameters except within the accepting
organization, distinguish individual from organization-authorized access, and
require deletion after termination. They also impose separate requirements on
outputs and exclude clinical use.

Google does not publish a SHA-256 beside the current object. A direct download
over a verified TLS connection was checked against the generation metadata,
exact size, zstd framing, and full zstd stream on 2026-09-02. Its computed
SHA-256, now pinned in the contract, is
`74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff`.
Every later copy of that object generation must match this digest.

## One-step artifact intake

1. Copy the matching acceptance templates to an owner-only directory outside
   Git. Review the live terms, replace the timestamp, select the truthful
   accepting role/scope, and change `accepted` to `true`. Do not add names,
   credentials, download links, or tokens. The shared-cache command requires
   `authorized-organization-representative` plus `organization-internal`; it
   rejects an individual-only receipt.
2. Obtain each exact artifact through its official process. Do not rename it.
3. Set paths through environment references so private filesystem locations do
   not appear in command history, then run the single intake-and-cache command:

```bash
export FS2_ACADEMIC_STATE=/secure/fs2-academic-assets
export FS2_AF3_FILE=/secure/intake/af3.bin.zst
export FS2_AF3_ACCEPTANCE=/secure/intake/alphafold3.acceptance.json
export FS2_PYROSETTA_FILE=/secure/intake/pyrosetta-2025.24+release.8e1e5e54f0-py310_0.conda
export FS2_PYROSETTA_ACCEPTANCE=/secure/intake/pyrosetta-bindcraft.acceptance.json
export FS2_ACADEMIC_GENERATION=intake-YYYYMMDD

export FS2_ACADEMIC_ASSET_STATE_DIR=${FS2_ACADEMIC_STATE}
academic-assets/scripts/ingest-approved-assets.sh
```

The command validates and stages immutable generation files with mode `0400`,
writes canonical receipts and pointers with mode `0600`, keeps the local root
at `0700`, and atomically activates the complete generation. It then creates or
reuses the task-owned PVC on the existing eu-north1 shared filesystem, streams
each file through a short-lived non-root CPU pod, independently rehashes the
cached bytes, records a non-secret cache receipt, and deletes the pod. It emits
only the readiness projection. Reusing a generation ID is rejected.

The agent cannot create the acceptance receipts on behalf of an individual or
organization. Until a truthful receipt is supplied, source bytes may be hash-
validated into an owner-only quarantine by omitting all acceptance arguments,
but `resolve`, private-cache evidence, image build, deployment, and semantic
readiness remain blocked. The cache may retain already acquired bytes by exact
digest, but it has no active readiness receipt. Once the user supplies both
receipts, the one-step command above rehashes those immutable cached bytes and
advances only to `MissingImage`.

`--state-dir` is also accepted, but the environment form is preferred. No
artifact URL or credential input exists; signed URLs therefore cannot enter
Terraform state, Git, process output, or receipts.

## Readiness and downstream evidence

Readiness is per native model and moves through these fail-closed states:

| State | Meaning |
| --- | --- |
| `InvalidContract` | The active generation belongs to a different contract revision and must be explicitly rotated. |
| `MissingLicenseAcceptance` | Exact pinned terms/source claims have not been affirmatively accepted. |
| `MissingArtifact` | Terms are accepted but no matching file is staged. |
| `MissingCache` | Content is verified locally but no matching same-region private-cache receipt exists. |
| `InvalidCacheReceipt` | Cache evidence does not match the artifact, license scope, or contracted infrastructure. |
| `MissingImage` | The private cache is verified but no exact runtime-image receipt exists. |
| `InvalidImageReceipt` | Image evidence is mismatched, public, redistributable, or embeds AF3 parameters. |
| `MissingDeployment` | The private image exists but no bound live resource UID exists. |
| `InvalidDeploymentReceipt` | Deployment evidence is not bound to the exact artifact/model/image chain. |
| `MissingSemanticReadiness` | Deployment exists but the exact semantic validator has not passed. |
| `InvalidSemanticReceipt` | Semantic evidence is not a passing validator bound to the exact runtime. |
| `Ready` | License, artifact, private cache, image, deployment, and semantic receipts form one exact chain. |

The admin/catalog-safe projection is stored as `readiness.json` in the active
private generation and is also emitted by `status`. It exposes digests and
states, never paths or acceptance material:

```bash
python3 academic-assets/scripts/academic_assets.py \
  --contract academic-assets/contracts/academic-assets.json \
  status --state-dir /secure/fs2-academic-assets
```

Cache, image, deployment, and semantic automation records immutable receipts
with `record`. Schemas are in `schemas/`. Every receipt must bind the active
artifact SHA-256 and exact contracted infrastructure identity; deployment and
semantic receipts must additionally bind the exact private image digest. A
different receipt requires a new ingestion generation.

## Optional private runtime layer

`scripts/build-private-layer.sh` copies the verified PyRosetta package into an
already digest-pinned runtime image through a BuildKit secret mount, pushes
only to the contracted eu-north1 private registry, requests provenance and an
SBOM, and records the returned OCI digest. It deliberately refuses:

- an individual-only acceptance;
- a non-digest base image;
- a target outside the task's private registry prefix;
- execution without `FS2_PARENT_INTEGRATION_APPROVED=yes`.

AlphaFold3 parameters are rejected by this wrapper and must remain an external
private-volume mount; they are never embedded in a runtime image.

The wrapper reads registry authentication only through the existing Docker
credential helper. It never accepts or prints a credential. The layer merely
places the exact PyRosetta conda package at its contracted runtime path; the
BindCraft runtime must install it offline. AlphaFold3 must point its model
directory at the external private-cache parameter file.

Do not run the image wrapper before parent integration review. Artifact staging
creates only the dedicated cache claim and short-lived loader pod; it does not
change a shared service, deploy a model runtime, or use a GPU.

## Rotation and rollback

Create a new immutable generation for any acceptance, source revision,
artifact, or runtime rotation. Valid old generations and image digests remain intact.
After validating the intended target, rollback changes only the atomic active
pointer:

```bash
python3 academic-assets/scripts/academic_assets.py \
  --contract academic-assets/contracts/academic-assets.json \
  rollback --state-dir /secure/fs2-academic-assets \
  --to-generation intake-previous
```

If an acceptance attribution was invalid or accepted terms terminate, activate
a replacement quarantined generation first, then remove the invalid inactive
generation while retaining a non-secret manifest tombstone:

```bash
python3 academic-assets/scripts/academic_assets.py \
  --contract academic-assets/contracts/academic-assets.json \
  revoke --state-dir /secure/fs2-academic-assets \
  --generation invalid-generation \
  --reason invalid-license-acceptance-attribution
```

Run all offline validation with `academic-assets/run_checks.sh`.
