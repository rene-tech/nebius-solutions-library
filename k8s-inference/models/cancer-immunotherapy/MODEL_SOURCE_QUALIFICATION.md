# Cancer Immunotherapy Model Source Qualification

**Retrieval date: 2026-09-02. Use class: academic, non-commercial proof of concept.
Target hardware: NVIDIA H100 SXM5 80GB, Hopper, compute capability 9.0
(`nvidia-h100-sxm5-80gb` in `catalog/profiles/accelerator-pools.json`).**

This is read-only research. Nothing here asserts that any model was deployed,
benchmarked or qualified on hardware. Where this document says a model fits the
H100, it distinguishes an upstream statement naming Hopper from an untested
expectation, and it never collapses the two.

The machine-readable form of everything below is
`model-source-qualification.json`, validated by
`model-source-qualification.schema.json` and
`tests/test_model_source_qualification.py`.

## 1. What the eight requested names resolve to

All eight resolved. Two carry a one-line confirmation question because the name
is generic or a newer generation now exists; neither blocks onboarding, because
each has a stated default.

| Requested name | Resolves to | State |
| --- | --- | --- |
| Proteina-Complexa | NVIDIA binder design model, three weight variants | Resolved |
| BoltzGen | All-atom universal binder design model from the Boltz team | Resolved |
| mosaic | Escalante Bio composite-objective design framework | Confirm name |
| BindCraft | AlphaFold2 hallucination binder pipeline | Resolved |
| RFdiffusion | The original RosettaCommons backbone diffusion model | Confirm generation |
| ESMFold2 | Biohub all-atom complex structure predictor | Resolved |
| ESMFold2-Fast | Biohub single-sequence inference-optimized sibling | Resolved |
| Protenix v2 | ByteDance `protenix-v2`, a real tagged release | Resolved; exact third-party mirror candidate verified, publisher-byte comparison unavailable |
| AlphaFold3 | Google DeepMind AlphaFold 3 | Resolved |

Four further models are recorded because the workload cannot run without them:
ProteinMPNN as a shared pipeline dependency, OpenFold3 as the operator-supplied
AlphaFold3 alternative, and FreeBindCraft as the PyRosetta-free comparison lane.

### The three names that were genuinely ambiguous

**ESMFold2 and ESMFold2-Fast were the cleanest resolution.** The slash in the
original request implied uncertainty, but Biohub publishes exactly two weight
repositories and they are named `ESMFold2` and `ESMFold2-Fast`. The customer's
two names map one to one onto them. The base variant supports optional
alignment conditioning and carries a chemical component dictionary for ligands
and modified residues; the Fast variant is single-sequence only and ships no
such dictionary. Both are MIT and ungated. Third-party re-uploads of both names
exist under an unaffiliated account and should be ignored.

**Proteina-Complexa is unambiguous but has three checkpoints.** The name matches
one NVIDIA model with separate weights for protein targets, small-molecule
targets and motif scaffolding. The question is not which model but which
pipelines are in scope.

**mosaic is the one real naming risk, and it is not a model.** It resolves with
high confidence to Escalante Bio's framework: its published case studies are
PD-L1 and IL7Ra minibinders, which sit squarely in cancer immunotherapy, and it
natively composes four other models on this same request list. But it is a
lowercase English word, so one line of confirmation is recorded rather than
assumed. Critically, mosaic ships no weights of its own. It is a JAX framework
for combining other models into a composite objective, so its licence exposure
is entirely determined by which components a given job loads.

## 2. Licensing and the exact one-time access steps

Because this is an academic proof of concept, the platform owner has recorded
operational PoC authorization for the academic PyRosetta and AlphaFold3 lanes.
The exact PyRosetta 2026.29 CPython 3.10 wheel is installed on the tenant-private
academic volume and has passed an offline import plus real pose-scoring test.
The exact Google AlphaFold3 parameter generation is installed on that separate
private plane and passed real H100 inference with the accepted r6 runtime image,
digest `sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e`.
Neither result says native BindCraft itself is deployed or ready.

**BindCraft's PyRosetta prerequisite is operationally available for this PoC.**
PyRosetta remains an unconditional import in the native pipeline, but its exact
installed tree has been integrity-checked and tested offline. Its wheel and
installed files remain licensed bytes owned by `academic-assets`; they are not
embedded in an image or copied into the public artifact cache. Formal Rosetta
and PyRosetta institutional acceptance remains a non-PoC advisory for
commercial or broader organizational use. FreeBindCraft stays
a separately named scientific comparison lane, never the native implementation.

**AlphaFold3's private parameters are operationally available for this PoC.**
The exact object was received directly from Google, privately installed, loaded,
and semantically tested on H100 with the digest-pinned r6 runtime. The parameters
remain restricted to the tenant-private academic volume and excluded from every
general multi-tenant cache and image. Formal institutional acceptance remains
pending as a later non-PoC advisory and must be completed by an authorized
representative before use outside the explicitly authorized PoC scope.

**The NVIDIA microservice route needs a registry key and the right entitlement
tier.** NVIDIA's product terms allow these microservices without a subscription
only on a workstation, and explicitly exclude a server servicing multiple users,
which is exactly what this cluster is. The separate free developer programme
covers research, application development and experimentation on up to sixteen
GPUs, which is the tier this academic work fits. Production or commercial use
would require an AI Enterprise subscription. This matters most for RFdiffusion,
where the microservice is the recommended route.

Everything else is clean. BoltzGen, mosaic, ProteinMPNN and both ESMFold2
variants are MIT. Protenix and OpenFold3 are Apache 2.0 on code and weights.
RFdiffusion is BSD, and its licence text says explicitly that it covers the
model weights as well as the source. Proteina-Complexa splits its licensing:
Apache 2.0 code with weights under the NVIDIA Open Model License, which states
the models are commercially usable.

## 3. Blockers, ranked by how much they change the plan

**Protenix v2 is obtainable only through an explicitly limited mirror path.**
The canonical ByteDance checkpoint still returns HTTP 403 from the operator
region and the publisher exposes no checksum, so publisher byte equivalence is
not knowable. The public `TMF001/protenix-v2-weights` repository is therefore
recorded as a third-party mirror at immutable commit
`653edab28103133512575365130916e3fd23ecc3`, never as the canonical source. Its
1,859,785,497-byte object hashes to SHA-256
`8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599` and
MD5 `49016ebf4775bf6b629bc4dc77b6673e`. Safe offline PyTorch inspection found
the top-level `model` mapping and matched all 4,174 keys, tensor shapes, and
464,442,431 parameters against exact v2 source revision
`2475421477ab414b571149ad4a875c390ff8a35d`. Its provenance state is precisely
`mirror-verified-not-publisher-byte-compared`; this is artifact qualification,
not an H100 semantic-readiness claim. The v1 checkpoint remains a separately
named comparison lane, not the implementation of v2.

**BindCraft's release tag is diverged and would ship the wrong code.** Comparing
the published tag to the default branch returns status diverged, twelve commits
ahead and four behind, and the tag resolves to a commit nine months older than
the release that names it. Pinning the tag is a silent correctness failure. The
manifest pins a commit instead. This is not a BindCraft-specific lesson: neither
Proteina-Complexa nor mosaic publishes any tag at all, RFdiffusion has not
tagged since 2023, and both ESMFold2 model cards document installing from a
moving branch.

**RFdiffusion's open image predates Hopper.** The shipped conda environment
pins CUDA 11.1 and the shipped Dockerfile pins CUDA 11.6. Neither targets
compute capability 9.0. The open route therefore needs a rebuild on a modern
CUDA base before it can run here at all, which is the main reason the
microservice is recommended for the mainline lane.

**Proteina-Complexa documents Ampere, not Hopper.** All three NVIDIA model cards
list supported hardware microarchitecture as Ampere only and test hardware as
A100. Nothing says H100 is unsupported, so this is an untested expectation, and
it must be proven by a real run before the model is called qualified.

**OpenFold3 cannot express covalent chemistry.** See the next section.

## 4. Is OpenFold3 an AlphaFold3 alternative?

Mostly yes, and it is the better default for this cluster, with one sharp
exception.

Where it wins: OpenFold3 is Apache 2.0 on both code and weights, with training
provenance tracing to public-domain structural data. There is no approval step,
no confidentiality obligation and no non-commercial ceiling, so the lane can
carry over to commercial work later, which the AlphaFold3 lane never can. Its
weights download unauthenticated from a public bucket. H100 is explicitly
supported with published latencies.

Where it loses, and this is the decisive question to put to the customer:
**OpenFold3 cannot represent covalent chemistry at inference.** Covalently
modified residues, cross-chain covalent bonds, covalently bound ligands and
polymeric ligands such as glycans are all listed by upstream as not yet
supported, and there is no equivalent of AlphaFold3's bonded-atom-pairs field in
its input schema. Simple non-canonical residue substitutions do work, so many
post-translational modifications are expressible as residue swaps. But
glycosylated antibodies and antibody-drug-conjugate linkers are not. If the
workload needs those, AlphaFold3 stops being optional.

On accuracy, upstream is candid that no reproduction has fully matched
AlphaFold3 across all modalities, with reported gaps concentrated in
nucleic-acid monomers and in protein-protein and protein-DNA interactions.

On migration cost: the two input schemas are semantically close but
syntactically different, and no converter ships upstream. A converter is a
modest piece of work for the supported subset, but it must reject rather than
silently drop any AlphaFold3 job that uses covalent bonds, because dropping a
bond changes the science without changing the output format.

## 5. H100 fit and runtime shape

Only three models on this list have H100 support documented by their own
upstream: AlphaFold3, OpenFold3, and RFdiffusion by way of its microservice.
BindCraft is documented by its project wiki, which publishes H100 per-trajectory
timings directly. Everything else is an untested expectation.

| Model | H100 fit | GPUs | Runtime shape | Route |
| --- | --- | --- | --- | --- |
| Proteina-Complexa | Untested, Ampere documented | 1 | Batch job | Custom container |
| BoltzGen | Untested, A100 timings published | 1 | Batch job | Custom container |
| mosaic | Untested | 1 | Batch job | Custom container |
| BindCraft | Documented | 1 | Batch job | Custom container |
| FreeBindCraft | Untested | 1 | Batch job | Custom container |
| RFdiffusion | Documented via microservice | 1 | Both | Existing NIM |
| ProteinMPNN | Untested, trivially small | 1 | Batch step | Open runtime |
| ESMFold2 | Untested | 1 | HTTP service | Open runtime |
| ESMFold2-Fast | Untested | 1 | HTTP service | Open runtime |
| Protenix v2 | Untested | 1 | Both | Open runtime |
| AlphaFold3 | Documented | 1 | Both | Open runtime |
| OpenFold3 | Documented | 1 | Both | Open runtime |

Three structural observations matter more than any single row.

**Not one of these models parallelises a single job across GPUs.** Every one of
them is one GPU per unit of work. Scale-out is many pods, never a bigger pod.
This should shape the batch scheduling design directly.

**Most of them are not services.** Five of the twelve are design campaigns that
produce ranked output over minutes to hours, and modelling them as low-latency
HTTP endpoints would be a category error. Only the two ESMFold2 variants fit a
conventional model server cleanly, which makes ESMFold2-Fast the natural choice
for an interactive demo lane.

**The binding constraint is frequently not the GPU.** BoltzGen needs 64 GB of
host memory and is reported to fail its analysis stage at 16 GB. BindCraft's
relax and scoring stages are CPU-bound. AlphaFold3's alignment pipeline needs
roughly 252 GB of database downloads unpacking to about 630 GB, up to a terabyte
of fast local disk and at least 64 GB of RAM, and never touches the GPU at all.
OpenFold3's equivalent pipeline ranges from about 330 GB to several terabytes.
For both, the correct decomposition is a CPU alignment job feeding a
single-GPU inference job, which both projects support directly through flags.
Section 8 records why this matters concretely here: the observed GPU nodes carry
no large local NVMe, so those databases cannot go on node-local disk and the
shared filesystem has to absorb them.

Two cluster observations from section 8 bear directly on this table. The
installed driver satisfies the RFdiffusion microservice but not the Boltz-2 one,
so a microservice route is not uniformly available. And the observed GPU node
memory and core counts are generous enough that the host-side requirements above
are comfortable, with the storage question being the real constraint.

A related warning for whoever designs the alignment stage: the public hosted
alignment services that these projects default to are not usable from a pod
fleet. One asks explicitly that it not be queried from multiple machines, and
another would send proprietary target sequences to a third party. Self-host or
precompute.

## 6. Semantic validators

Every model has a concrete fixture and scientific acceptance criteria in the
manifest. The design principle throughout is that a transport-level success
proves nothing: these pipelines fail silently and plausibly, returning
well-formed output that is scientifically worthless. Three fixtures are worth
calling out because they detect exactly that.

**BoltzGen's composition filters are a degenerate-output detector.** Diffusion
binder models fail characteristically by emitting poly-alanine or poly-glycine
backbones that score well geometrically while not being real proteins. A run can
complete every step, write every file and still be worthless. Assert on the
metrics, never on the exit code.

**OpenFold3's fixture has a floor as well as a ceiling.** Upstream publishes
both the expected accuracy with alignments and the expected inaccuracy without
them. A ceiling-only test still passes when the alignment path has silently
died; the floor catches that. This is the single best-designed acceptance
fixture of any model reviewed here, and it is worth copying elsewhere.

**BindCraft's Rosetta-derived columns double as a licence probe.** If interface
residue count and buried surface area come back zero or absent, PyRosetta did
not initialise. That is the cheapest possible detector for the licence failure
mode and it belongs in continuous integration.

Two fixtures are directly on-target for the customer's science rather than
generic: Proteina-Complexa ships a PD-L1 design task, and BoltzGen ships both a
PD-L1 antibody example and a peptide design against a melanoma antigen presented
on a class-one MHC molecule.

## 7. A finding the fast-start task needs

The epic warns against ever labelling a fallback as a GPU snapshot. Research
done for this task supports that warning with specifics.

**NVIDIA does not document GPU memory snapshot or checkpoint restore as a
supported feature of any inference microservice, Helm chart, or the operator.**
Two mechanisms exist at other layers. The CUDA checkpoint tool checkpoints and
restores CUDA state within a running process, but upstream describes it as still
actively developed and it does not support unified or shared memory. A
platform-level snapshot capability also exists, but it is in preview, x86 only,
single GPU, restricted to large language model workers on two specific serving
backends, and requires a privileged daemon set. None of the biology
microservices are language model workers.

What does exist and works is more mundane: a pre-populated cache on a
persistent volume, which is the only supported way to skip the first-run model
download. The caches are large, and one microservice documents needing hours to
populate on a typical connection. Two open models offer something better still:
BoltzGen's Dockerfile takes a build argument that bakes its roughly six
gigabytes of weights into the image, removing the download from the startup path
entirely.

## 8. Read-only discovery of what already exists

Performed against the operator-provided acceptance kubeconfig, context
`k8s-inference-h100`, using only `get` and `list`. Nothing was created,
patched or deleted. Secrets were enumerated by name and type only; no secret
value was read.

Cluster state and build-host state are reported separately below, because they
are different systems and a credential or cache on one implies nothing about the
other.

### The shared H100 cluster, observed directly

Project `project-e00rene`, region `eu-north1`.

| Observation | Value |
| --- | --- |
| GPU nodes Ready | 2, each with 8 allocatable GPUs |
| Accelerator class | `nvidia-h100-sxm5-80gb` |
| Pool | `h100-reserved-8x`, capacity source `capacity-block`, type `regular` |
| NVIDIA driver | 580.159.04 |
| GPUs in use | 1 of 16, by one running Qwen pod in `fs2-models` |
| Model cache volumes | 2 x 128Gi ReadWriteMany on `csi-mounted-fs-path-sc` |
| GPU node ephemeral storage | about 277 GiB, node root disk, no large local NVMe |
| Pull secrets in `fs2-models` | none; the namespace holds no secrets at all |

Four of these change decisions and are not merely inventory.

**The driver version splits the microservice lanes.** At 580.159.04 the cluster
meets the RFdiffusion microservice minimum of 580, so that lane is driver-ready
today. It does not meet the Boltz-2 microservice, which documents a minimum of
590.44.01 with CUDA 13.1. Any Boltz-2 microservice lane would require upgrading
the driver on a shared cluster, which is a change to other tenants and must not
be assumed available.

**Fifteen of sixteen H100s are free right now**, which is ample for this
workload given that not one of these models parallelises a single job across
GPUs. That is a point-in-time reading, not a reservation, and the shared-cluster
policy of preserving other tenants still applies.

**The shared-cache pattern already exists and should be extended, not
reinvented.** Two 128Gi ReadWriteMany claims already back the Cosmos and Qwen
lanes on the shared filesystem storage class.

**There is no large local NVMe on the GPU nodes**, which collides directly with
the alignment databases. AlphaFold3 wants on the order of a terabyte of fast
local disk and OpenFold3 wants from roughly 330 GB upwards. Neither fits in
about 277 GiB of node root disk, so those databases have to live on the shared
filesystem, and the existing 128Gi claim size is nowhere near sufficient. This
is the single largest infrastructure commitment implied by the request list, and
it is a capacity conversation to have before either lane is scheduled.

### The image pull path

The three existing model deployments pull digest-pinned images from the
in-project registry at `cr.eu-north1.nebius.cloud` and declare no image pull
secrets, so in-project images pull without an explicit credential. Mirroring
third-party images into that registry is the established pattern here and is the
right home for the RFdiffusion image, whether it comes from the microservice or
from a rebuilt open image.

On NVIDIA registry access, stated precisely rather than generally: a read-only
inspection of the Boltz-2 microservice repository **from this build host**,
using the docker credential stored in the host's docker config, returned HTTP
403 on the registry ping. Separately, the `fs2-models` namespace on the cluster
contains no secrets, so no pull secret exists there either. Those are two
distinct facts and neither one establishes that the microservices are
unavailable to this cluster. **Whether an in-cluster pull succeeds is untested**,
because testing it would require creating a pull secret, which is a mutation
this read-only task did not perform. It was also not diagnosed further, because
narrowing down whether the 403 reflects an expired credential, a missing
per-organisation terms acceptance or an entitlement scope limit would have meant
trying other credentials, which policy forbids without asking first.

The concrete step is therefore: obtain a valid NGC personal or service key,
accept the terms once for the specific microservice at the organisation level,
create it as a pull secret in the target namespace, and only then conclude
anything about in-cluster availability.

### The build host

The local model cache on this build host holds only unrelated media and language
models. No weights for anything on this request list are cached locally, so
every artifact is a fresh fetch. This says nothing about cluster volumes, which
are the separate observation above. A model hub credential file is present,
which covers the ungated artifacts, and most artifacts here need no credential
at all.

## 9. Method

Every revision, licence and gate in the manifest came from a primary source
retrieved on 2026-09-02: upstream repositories through the authenticated forge
API, published package indexes, model hub metadata endpoints, and vendor
documentation. Search results were used only to locate primary sources, never as
evidence.

Five claims consequential enough to change a recommendation were verified a
second time by direct probe rather than taken from any single report: the
Protenix v2 publisher checkpoint returning 403 against its siblings returning
206, the exact mirror byte identity and source-architecture match, the
AlphaFold3 parameter file now being directly reachable, the BindCraft tag being
diverged from its branch, and the OpenFold3 checkpoints being reachable
unauthenticated while the withdrawn legacy checkpoint returns 404.

Claims that could not be verified are marked as such in the manifest rather than
smoothed over. The most significant is that no wet-lab comparison has been
published between PyRosetta-based BindCraft and its bypass fork, which its own
authors state plainly. That is the honest residual risk in the PyRosetta-free
fallback lane, and it is why that lane is recorded as a comparison rather than
an equivalent.
