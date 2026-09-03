# AlphaFold 3 academic runtime

A digest-pinned, nonroot AlphaFold 3 v3.0.4 runtime for the authorized academic
proof of concept. The image contains the upstream Apache-2.0 source, its Python
environment, HMMER and the Chemical Component Dictionary derived data. It
contains **no model parameters and no reference-database bytes**.

This directory is self-contained. It is a clean successor and deliberately
shares no history with the earlier multi-model secondary-images work.

## Identity

| What | Value |
| --- | --- |
| Upstream | `google-deepmind/alphafold3`, tag `v3.0.4` |
| Commit | `85c4d20505fd5cef05eac22b534d4e793971ae69` |
| Tree | `efa1a376c9cf94d517d70e68425bc1ed3b17a570` |
| Base image | `nvidia/cuda:12.6.3-base-ubuntu24.04` by digest |
| Platform | `linux/amd64` |
| Default user | `1001:1001`, nonroot |

The tag `v3.0.4` resolves to exactly the pinned commit. The build fetches that
one commit and asserts both the commit id and the tree id before any upstream
byte is used, so an equal id is an equal tree.

## What is not in the image

The AlphaFold 3 parameters are licensed under the AlphaFold 3 Weights Terms of
Use, are obtained by the operator directly from Google, and are never embedded,
layered, copied, cached or exported. The public reference databases are also
external. Three independent checks enforce this:

1. The build fails if the upstream source tree carries a parameter-shaped or
   database-shaped file.
2. The build fails if any such file exists anywhere in the final filesystem.
3. `build.py inspect` walks every layer of the built OCI archive and fails on
   any offender, so the guarantee holds for the artefact that would be
   published rather than only at build time.

The entrypoint repeats the check at run time before it touches anything.

## The two stages

Preprocessing and inference are separate stages and a single stage never holds
both bindings. That is what keeps a GPU from sitting idle during a CPU-only
database search, and what keeps licensed bytes away from a stage that has no
reason to read them.

| | CPU data stage | GPU inference stage |
| --- | --- | --- |
| GPUs | 0 | 1 |
| Parameters | not bound | `/models/af3.bin.zst`, read-only |
| Reference tree | read-only under `/reference-data` | not bound |
| Added flags | `--norun_inference`, both MSA thread flags | `--norun_data_pipeline` |
| Consumes | published reference tree | the CPU stage's immutable handoff |

The runtime refuses a stage that declares both bindings, a CPU stage with
parameters mounted, or a GPU stage with a reference tree mounted.

## Parameter binding

`academic-assets` owns the parameter identity; this runtime consumes it and
never redefines it. The canonical binding is a subPath file mount:

- claim `academic-assets-runtime-rwx` in namespace `fs2-academic-poc`
- source subPath `alphafold3/af3.bin.zst`
- container path `/models/af3.bin.zst`, read-only, so `--model_dir=/models`
- SHA-256 `74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff`
- 1020545840 bytes

The pod must set `supplementalGroups: [65532]` and must **not** set `fsGroup`,
which would rewrite the delivered modes and make licensed bytes group writable.

Before execution the runtime checks size, then the zstd magic, then the full
SHA-256, and refuses to continue on any mismatch. It also requires the resolved
model directory to expose exactly one parameter object, because AlphaFold 3
selects its model by scanning that directory.

## Reference-data binding

The reference-data worker owns publication. This runtime only consumes an
already published tree and fails closed until that happens. It reads the
producer's terminal receipt, schema
`fs2-serve.nebius.ai/reference-data-terminal-receipt/v1`, whose `storage` block
is exactly `host_root`, `mount_path`, `dataset_sub_path`, `read_only` and whose
`content` block carries the aggregate tree digest and an **independent**
manifest digest.

**One read-only root.** The stage mounts the shared reference filesystem
`/mnt/fs2-reference-data/data` at `/reference-data`, and both of these resolve
beneath that single root:

- `<mount_path>/<dataset_sub_path>` is the database root
- `<mount_path>/manifests/sha256/<manifest_sha256>.json` is the manifest

Mounting only the dataset is refused, because the manifest could then not be
verified against the tree it describes.

The binding rests on the mounted filesystem, not on any string in the receipt:

1. `database_root` is derived from the receipt, never hand-assembled.
2. That directory's **full name** must equal the aggregate tree digest. A
   revision-only match or a truncated digest prefix is rejected.
3. The `.fs2-manifest-sha256` marker inside it must equal the receipt's
   manifest digest. The marker exists only for a complete tree, so it is also
   the readiness signal.
4. The sibling manifest must exist and must recompute to that digest under the
   publisher's exact canonicalization, and its tree and inventory identities
   must agree with the receipt.

Near-miss field names are refused rather than ignored, because accepting one
would let this runtime bind a digest nobody produced. `published_manifest_sha256`,
`source_sub_path`, `published_tree_sha256`, `manifest_digest` and
`shared_filesystem_uri` are all rejected.

The terminal receipt and the controller's preprocess request are different
documents. The producer owns that transform; this runtime mirrors it in
`ReferenceBinding.preprocess_reference_data` because it cannot import the
producer inside the image, and the integration tests assert both sides produce
identical output for a producer-generated receipt.

## CPU envelope

AlphaFold 3 defaults `--jackhmmer_n_cpu` and `--nhmmer_n_cpu` to
`min(cpu_count, 8)`, read from the **node** rather than from the pod. That is
wrong in both directions. The reference-data plane declares the AlphaFold 3
preprocessing stage as 16 CPU, 64Gi memory and 32Gi ephemeral storage and
refuses a smaller request, so the node-derived default would silently cap both
MSA tools at eight and waste half the stage; on a stage below eight CPUs the
same default would oversubscribe the cgroup instead.

The data stage therefore always passes both flags explicitly from the
controller-frozen thread count, and rejects a value that exceeds the stage's CPU
request.

## Caches

`/cache/alphafold3` holds XLA and Triton compilation artefacts. It is an
auxiliary compiler cache. It is **not a GPU snapshot** and must never be
reported as one. Image-local content is the L1 level and no higher level is
claimed without measured evidence from the fast-start qualification task.

The cache is optional. When its directories are not writable the run continues
without a cache and the receipt records the degradation, so an uncached run can
never be mistaken for a cached one.

## Command and IO contract

`contracts/af3-command-io-contract.json` is the single machine-readable command
surface for `data` and `inference`: exact runtime arguments, the upstream argv
each stage composes, input and output files, the root layout, and the result
envelope with its exit codes. It is **generated** from the runtime's own argv
composers by `python3 build.py contract`, and `run_checks.sh` fails if the
committed copy drifts from the implementation.

`fixtures/` holds controller-consumable fixtures generated by
`python3 fixtures/generate.py`: a producer-generated terminal receipt and its
manifest, the derived preprocess `reference_data` block, planned receipts for
both stages, and the fail-closed envelope.

There is no `fs2-run-alphafold3` entrypoint or flag set. An adapter targeting it
cannot invoke this image; the contract records that explicitly rather than
adding a second, untested command surface as an alias.

## Usage

```bash
# Identity and hygiene only. No GPU, no parameters. Safe as a readiness gate.
docker run --rm <image> verify

# Import the distribution, assert 3.0.4, report visible JAX devices.
docker run --rm <image> smoke

# Real semantic check: verify the parameter identity, then load it through
# AlphaFold 3's own official loader.
docker run --rm --gpus 1 -v <claim>:/models/af3.bin.zst:ro <image> params-load

# Compose a stage's argv without running it.
docker run --rm <image> plan --stage inference --json-path /handoff/data.json
```

Every mode emits a JSON receipt on stdout against
`schemas/af3-runtime-receipt.schema.json`. A contract failure exits `2` with a
`FAIL` receipt; any other non-zero code is `run_alphafold.py`'s own, passed
through unchanged.

## Build and publish

The provenance a build records has to name source a reviewer can check out, so
the flow is two-phase and the order matters:

1. Commit the runtime source. The build refuses a dirty context and refuses a
   requested revision that is not `HEAD`.
2. Build from that commit. Afterwards the build reads the `vcs:revision` out of
   the SLSA provenance and fails unless it equals the commit it built from, so
   an image whose provenance points at amended-away source can never be handed
   back. BuildKit's `resolvedDependencies` records base images but not the local
   context, so the exact context files are digested into `context_sha256` too.
3. Publish, run hardware acceptance, then record the lock and evidence in a
   second, evidence-only commit.

Never amend the source commit after building: the attestation would then name a
commit that no longer exists.

```bash
./run_checks.sh                                     # offline gate

python3 build.py check
python3 build.py build   --builder <buildx-builder> --oci-file <out>.oci \
                         --source-revision "$(git rev-parse HEAD)" \
                         --receipt <out>.build-receipt.json
python3 build.py inspect --oci-file <out>.oci --receipt <out>.image-receipt.json
python3 build.py smoke   --oci-file <out>.oci --receipt <out>.smoke-receipt.json
python3 build.py publish --oci-file <out>.oci --repository <registry-path> \
                         --receipt <out>.publication-receipt.json
python3 build.py lock    --build-receipt ... --hygiene-receipt ... \
                         --smoke-receipt ... --publication-receipt ...
```

Building writes a local OCI archive with SPDX and SLSA provenance attestations
and nothing else. Publishing is a separate reviewed action that refuses to
overwrite an existing tag. A local tag is not a registry digest and a build
receipt is not a deployment.

The concrete registry account path is a deploy-time binding and is never
committed; the immutable digest in `contracts/af3-image-lock.json` is the
authoritative identity. Receipts are private and are gitignored.

## Layout

| Path | Purpose |
| --- | --- |
| `Dockerfile` | four-stage build: pinned source, HMMER, environment, runtime |
| `runtime/af3_runtime.py` | fail-closed entrypoint: identity, stages, argv, receipts |
| `build.py` | check, build, inspect, smoke, lock, publish |
| `contracts/` | source lock, parameter binding, reference binding, handoff |
| `schemas/` | receipt and image-lock JSON Schemas for consumers |
| `contracts/af3-command-io-contract.json` | generated command and IO surface |
| `fixtures/` | generated fixtures for controller tests, plus their generator |
| `tests/` | contract, runtime, command-IO, provenance and producer-interoperability tests |

## Ownership

This task owns the runtime image, its locks, its entrypoint and its contracts.
Submission, adapters, the scientific controller, Kueue queues, Terraform and the
admin surface are owned by other tasks, which consume
`contracts/af3-runtime-handoff.json`. Files owned by those tasks are not edited
here.

## Provenance

`contracts/af3-image-lock.json` binds four things to the published image: the
upstream revision, the repository commit the image was built from, the
`vcs:revision` the SLSA provenance recorded, and a digest over the exact build
context. The build fails if the first two disagree with the third.

The lock is a build result, so it is absent from a source commit and lands in
the evidence commit that follows. The lock tests skip while it is absent rather
than pretending to have checked it.

## Readiness

AlphaFold 3 is **not** ready, deployed or qualified.

- [x] runtime image built with SPDX and SLSA provenance, proven free of licensed
      and database payload by a layer walk, and published to the project
      registry by immutable digest
- [ ] reference-data worker publishes the bundle and its terminal receipt
- [ ] controller route and submission wiring landed by their owning tasks
- [ ] real H100 parameter-load and inference semantic acceptance recorded

Consume the image by the digest in `contracts/af3-image-lock.json`. The tag
`3.0.4-85c4d205` in the same repository is a **superseded** publication from an
earlier iteration and must not be deployed; the tag named after the upstream
commit belongs to another task's retained evidence image and is not this
runtime. `contracts/af3-runtime-handoff.json` records all of them.
