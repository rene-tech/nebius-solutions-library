# Native academic BindCraft runtime image

This package builds one `linux/amd64` image: the native PyRosetta BindCraft
lane, which is the required lane for the cancer-immunotherapy workload. It is
not a model bundle. The image contains the pinned BindCraft source and its
CUDA 12.1 / JAX stack and nothing else — no PyRosetta, no AlphaFold2 parameters,
and neither ColabDesign ProteinMPNN weights directory. Everything the model
reads arrives at run time and is verified before any model code executes.

The open PyRosetta-free fallback, RFdiffusion and ProteinMPNN were built by an
earlier mixed package and are deliberately not here. This tree is the final
BindCraft image, its runtime source, its immutable lock and receipt, its
qualification evidence, and the tests and docs for exactly that.

| | |
|---|---|
| Source | BindCraft `7cd4ace1b7407adf66a50dfefa47de2270f5e4a9`, archive `cada0f51…` |
| Base | `pytorch/pytorch@sha256:0279f7aa…` (PyTorch 2.3.0, CUDA 12.1, Python 3.10.14) |
| Final tag | `…/fs2-models/bindcraft:7cd4ace1b7407adf66a50dfefa47de2270f5e4a9-cuda121-r17` |
| Attestations | SPDX SBOM and SLSA provenance, attesting a revision reachable from a pushed branch |
| Qualification | offline-qualified; the live H100 semantic acceptance has **not** run |

## Academic PoC authorization

The owner has authorized the free academic PyRosetta path for this proof of
concept, and the image is built around exactly that authorization:

* The exact private **PyRosetta 2026.29** installed tree is consumed
  **read-only** from the tenant-private claim. The runtime verifies it, imports
  from it, and never writes to it.
* Licensed bytes are **never embedded** in the image and never placed in a
  public cache. The image is scanned for an importable `pyrosetta` and for model
  artifacts on every canary, and publication fails if either appears.
* **No per-request license receipt is required.** Admission is
  `AdmittedNoPerRequestLicenseReceipt`; the authorization and install receipt
  digests are deployment metadata, not request fields and not init gates.
* Broader or commercial use beyond this PoC is **advisory** and out of scope
  here. Nothing in this package grants or implies redistribution rights.

The mounted object is the canonical `ArtifactMaterialization`
`bindcraft-pyrosetta-installed-tree`: a 3,287,122,494-byte installed tree whose
`fs2-tree-manifest/v1` identity is
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`. That digest
is **pinned inside the image**, because the licensed tree is what the image is
licensed around: a run declaring a different identity for that role is refused
outright. The 1,667,097,173-byte source wheel (`4383d8d1…`) remains distinct
provenance and is not what ordinary runs consume.

## The four external trees

The four trees do **not** share a backing store. The three public ones are
immutable generations on the reference-data filesystem, reached by `hostPath` on
nodes that carry it; only the licensed PyRosetta tree lives on the private
academic claim. The handoff therefore supplies each tree's own volume source,
and the renderer refuses a handoff that serves the licensed tree from a public
volume or a public tree from the private claim.

| Role | Mounted at | Backing store | Identity algorithm | Authority |
|---|---|---|---|---|
| `pyrosetta-site-packages` | `/opt/fs2/academic/pyrosetta-bindcraft/site-packages` | private academic claim | `fs2-tree-manifest/v1` | pinned in the image |
| `alphafold2-params` | `/models/alphafold2` | reference-plane `hostPath` | `fs2-flat-tree-inventory/v1` | declared per run |
| `colabdesign-mpnn-weights-vanilla` | `…/colabdesign/mpnn/weights` | reference-plane `hostPath` | `fs2-flat-tree-inventory/v1` | declared per run |
| `colabdesign-mpnn-weights-soluble` | `…/colabdesign/mpnn/weights_soluble` | reference-plane `hostPath` | `fs2-flat-tree-inventory/v1` | declared per run |

Because the public trees are `hostPath`, both Jobs carry the handoff's node
selector: a Pod that lands on a node without the reference-data filesystem would
mount an empty directory rather than fail loudly.

`runtime/tree_identity.py` implements both algorithms and keeps each
byte-identical to the component that publishes the tree it guards:
`fs2-tree-manifest/v1` to `academic-assets/scripts/install_tree.py`, which
installs the licensed tree, and `fs2-flat-tree-inventory/v1` to the
scientific-localization staging receipts. A digest only this runtime could
reproduce would prove nothing about the tree the publisher actually shipped.

Every admitted tree is read in full. That is affordable and measured on the
eu-north1 shared filesystem — 3.29 GB of PyRosetta in 14.85 s, 5.59 GB of
AlphaFold2 in 5.36 s, each 26 MB MPNN tree in under 0.04 s, about 20 s in
total — against trajectories that run for minutes. No metadata-only shortcut is
offered, because a size-and-shape check passes on a tree whose contents were
swapped. Both MPNN roots must additionally be the directories `colabdesign.mpnn`
itself imports, so a declared root the model would never read cannot be admitted.

**Tree locations are inputs, never constants.** `FS2_BINDCRAFT_EXTERNAL_TREES`
points at an `fs2.nebius.ai/bindcraft-external-tree-admission/v1` document giving
each role's root and expected identity, so a re-publication that moves paths
without changing bytes needs no rebuild.

## Controller interface

Both `run-trajectory` and `aggregate` require
`--runtime-localization-marker <absolute path>`, because the shared controller
rejects any runtime-artifact stage whose argv omits
`<working directory>/.fs2/runtime-localization.json`. The wrapper reads it: the
controller owns the file's schema, so only an absolute path to a readable JSON
object agreeing with `FS2_RUNTIME_LOCALIZATION_MARKER` is required, and its path,
size and SHA-256 are recorded. A `generation` the marker declares is
cross-checked against the mounted trees, and `aggregate` applies the same check
across shards — the only place separately-scheduled Pods can be compared.

The ProteinMPNN lane is a request parameter, not an image constant:
`FS2_BINDCRAFT_MPNN_WEIGHTS` selects `original` or `soluble`, and with it unset
the SHA-256-pinned advanced template decides.

Two things the wrapper deliberately does not verify, so a consumer is not misled:
the admission document's `artifact_id` is required to be a non-empty string but
is never matched against an expected value, and `request.input_manifest.sha256`
is not checked, so both documents' on-disk formatting is free. The bytes that
matter scientifically are still gated: the materialized target structure's size
and SHA-256 are verified against the manifest entry before design starts.

## Why r17, and what each predecessor got wrong

`r12` through `r16` are superseded and must not be consumed. Tags are never
overwritten; the number advances because something in the contract changed.

* `r12` ran its semantic workflow under `no_filters`. Its accepted design's mean
  pLDDT of 0.79 would not pass the production filter set.
* `r14` read `Average_InterfaceResidues` and `Average_BuriedSASA`, which upstream
  never writes, each with a `"0"` default — so every accepted design reported
  zero interface residues and zero buried area. It also reported a hard-coded
  `hotspot_geometry_validated`, and overrode the pinned template with one
  validation recycle and one sampled MPNN sequence where production uses three
  and twenty.
* `r15` corrected all of that and added the four-tree admission gate, but
  predates the controller's marker flag.
* `r16` was behaviourally correct but not reconstructible. Its SLSA provenance
  attests revision `ce3ca6cd`, which survived only on a superseded branch, and
  it carried an `adapter.commit` label naming `3475ce0e`, rebased away and
  reachable from nothing. Nobody could resolve either back to source. It was
  also built from a working tree holding uncommitted edits.

`r17` fixes the provenance: the publisher refuses to build from a dirty tree,
refuses a revision not reachable from a pushed branch, re-checks the build
inputs afterwards so nothing changed mid-build, and then reads the published
SLSA provenance back and fails unless the attested revision and context are the
ones it built. The adapter wrapper is bound by SHA-256 rather than by a commit,
because a content digest cannot dangle.

From `r16` onward the runtime reads the columns upstream actually writes
(`Average_n_InterfaceResidues`, `Average_dSASA`,
`Average_Binder_Energy_Score`, `Average_Hotspot_RMSD`) and rejects a run whose
statistics are missing or non-numeric rather than substituting a zero. It
measures hotspot geometry from the accepted complex at upstream's own 4.0 Å
contact criterion, because `target_hotspot_residues` is a loss preference and
the interface residues upstream records are binder-side, so no upstream column
answers whether the binder reached the requested site.

## Build and publish

```bash
python3 build_images.py plan                    # exact plan
python3 build_images.py check-targets           # assert the target tag is absent
python3 build_images.py build                   # local build, no attestations
python3 build_images.py push                    # non-overwriting publish + verify + receipt
```

`push` is fail-closed on provenance as well as on content. It refuses to
overwrite an existing tag; refuses to build unless the source tree is clean and
`HEAD` is reachable from a pushed branch; builds with SPDX and SLSA
attestations; re-hashes every build input afterwards and refuses if any changed
during the build; re-pulls by digest; runs the artifact-free canary and both
batch subcommands; reads the published provenance back and refuses unless the
attested revision and context path are the ones it built; and only then writes
`evidence/published-images.json`. **Commit and push before publishing** — an
unpushed revision is rejected by design.

The image consumes the adapter's published `bindcraft-batch` wrapper from
`models/structure/runtime/bindcraft-native/bin/` through a read-only BuildKit
build context. That single file is the only thing this package needs from the
adapter tree, and it is carried alone rather than vendoring an adapter this task
does not own.

`runtime/runtime_entrypoint.py` is the shared outer entrypoint and still names
the split-out runtimes in its import and artifact-scan tables. That is
intentional: it is byte-identical to the file that produced the published r16
digest, and trimming it would break that match for no behavioural gain, since
this image only ever sets `FS2_RUNTIME_NAME=bindcraft-academic`. A test pins the
SHA-256 of all five in-image files against the qualification evidence so the
match cannot be lost by accident.

## Direct live H100 qualification before private promotion

The public AF2 and MPNN trees must have their immutable generation directories
and terminal in-generation markers before a direct acceptance starts. The
private PyRosetta installed tree is different: r18 pins its recursive identity
inside the image, reads every byte before import, and records the mounted root's
ownership. A missing localization marker for that already repaired canonical
claim tree therefore does not block the direct semantic proof.

Pass `--direct-live-canonical-pyrosetta` to the renderer to mount only that
private role from `pyrosetta-bindcraft/site-packages`; the three public roles
remain on the exact `/generations/.../sha256/...` paths from the accepted
handoff. The private role's generation is deliberately empty in the runtime
marker so this qualification cannot be mistaken for catalog localization or
route readiness. Normal rendering stays fail-closed until every handoff
generation is published.

## Live H100 semantic acceptance — not yet run

**No current-digest H100 success is claimed.** r16 is offline-qualified: 56
contract tests, the artifact-free image canary, byte-for-byte source identity
against the pulled digest, four in-image fail-closed proofs, and a server-side
dry run of the rendered Job against `k8s-inference-h100`.

The production run is blocked on the canonical immutable four-tree localization
generation. The mutable `scientific-localization/public/<artifact_id>` paths must
not be bound. As of the last check the claim has no `/sha256` generation root, an
empty `markers` directory, and only the superseded three-tree receipts.

When the generation lands, the run is one command:

```bash
R=qualification/render_semantic_job.py
COMMON="--handoff <four-tree-handoff.json> --image <r17 digest reference>
        --run-id <run> --job-name <job> --workspace-claim <durable claim>"
python3 $R $COMMON --direct-live-canonical-pyrosetta --stage design \
  | kubectl apply -f -   # GPU
# wait for the design Job to succeed, then:
python3 $R $COMMON --direct-live-canonical-pyrosetta --stage aggregate \
  | kubectl apply -f -   # CPU only
```

One stage per Job. Running the design as an init container of the aggregate's
Pod held the accelerator for the whole Pod lifetime, so a GPU sat idle through
the CPU-only aggregation. Split, the GPU is released when design ends; the shard
output survives on the durable workspace claim, and the aggregate re-verifies
every handed-off artifact against the digest the design stage published before
using it. Both stages enter the image through its outer entrypoint and carry the
pinned `default_4stage_multimer.json` and `default_filters.json` digests.

Each Pod has one small non-GPU init container that copies the four ConfigMap
control documents into an `emptyDir`. Kubernetes projects ConfigMap keys as
symlinks, while r18 intentionally refuses symlinked localization and tree
admission documents; the copy makes them regular read-only files without
relaxing the image gate. The design itself remains the GPU main container and
the aggregate remains a separate CPU-only Job.

The direct runner also prepares only its run directory on the task-owned empty
workspace claim with a short root init. It deliberately does not set Pod
`fsGroup`: that would apply volume ownership to the read-only academic claim as
well and could recursively damage the repaired PyRosetta ownership contract.
The root init mounts no model or academic volume.

Passing `default_filters.json` is the semantic bar, not a formality. Its 54
active thresholds require `Average_n_InterfaceResidues` ≥ 7, `Average_dSASA` ≥ 1,
`Average_dG` ≤ 0, `Average_Binder_Energy_Score` ≤ 0, `Average_Hotspot_RMSD` ≤ 6,
`Average_ShapeComplementarity` ≥ 0.6, `Average_pLDDT` ≥ 0.8 and
`Average_i_pTM` ≥ 0.5 — so a design that passes has, by construction, non-zero
interface residues, non-zero buried area and a non-zero PyRosetta score.

### The acceptance receipt must say which claim state it ran against

The academic claim is currently **non-conforming**. Its published delivery
contract is `asset_gid` 65532 with supplemental-group access, but the tree is
owned by group 10001 because pods mounting it with `fsGroup: 10001` had the
kubelet recursively chown it. Repair belongs to the academic-assets owner; four
tasks have hit it.

This matters for evidence, not for whether the run works. The Pod runs as
`10001:10001` with supplemental group 65532, so it reaches a repaired tree
through the supplemental group and the damaged tree through its primary group —
it passes either way. A green run therefore proves the semantics and proves
nothing about the access contract. The acceptance receipt must record the uid,
gid and mode of all four admitted tree roots, measured at run time, as a
first-class field; a conformance claim may only be made when that shows gid
65532.

Two requirements the handoff must satisfy, both cheap now and expensive after
publication:

* A terminal marker written **inside** a tree root changes that tree's
  `fs2-flat-tree-inventory/v1` digest, because the inventory enumerates every
  regular file in the flat root. Markers must live outside the tree roots.
* The AlphaFold2 root must keep a `manifest.json` declaring `artifact_kind`
  `bindcraft-af2-params` and `source_revision` `7cd4ace1…`, because the shared
  outer entrypoint's artifact gate reads it on every non-smoke command.

Full detail is in `evidence/native-final-image-qualification.json`.
