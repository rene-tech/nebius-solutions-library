# Cold-start optimization implementation

This implementation turns the post-acceptance spike into an executable,
fail-closed benchmark path. It does not promote a snapshot mechanism and it
does not alter a live cluster by itself. Conventional startup remains the only
active default for all 16 canonical routes.

## Evidence-led priority

The source baseline is the successful `r20260828d` all-hot timing receipt with
SHA-256 `05f8100cbbf94df1b9e6432392c52eb6aa5ec2d4c29086057a5f9ecd68696b79`,
captured from source commit `cf152f7b294f9a2ffee8401dee2d3239d1040400`
and tree `694bebdae74c29d8df9764ae6f060ce5161465d7`.

| Priority | Model | Pod created to Ready | Dominant observed interval | First candidate |
| --- | --- | ---: | --- | --- |
| 1 | Evo2-40B | 3,092 s | `materialize-checkpoint` ran for 1,805 s | exact durable shared-cache acquisition to a fresh model PVC |
| 2 | GLM-5.2-FP8 | 2,955 s | at least 2,498 s in the vLLM container after scheduling | catalog-rendered exact SFS artifact path instead of download into an empty PVC |

These are one-run baselines, not performance claims. A candidate needs at
least three alternating exploratory attempts and 20 attempts per arm for the
closed promotion receipt.

## Implemented contracts

`cold-start-optimization-matrix.json` is the single machine matrix. It is
cross-checked against the full-catalog model profile, route inventory, and
semantic contract, so deleting, renaming, or silently omitting a model fails
validation. Every model has explicit flags for:

- conventional startup;
- durable shared cache;
- node-local NVMe localization;
- native OCI ImageVolume;
- OCI modelcar;
- CUDA/CRIU snapshot; and
- Dynamo snapshot.

The flags have only four states: `active`, `eligible-experiment`, `blocked`,
and `not-applicable`. Only conventional startup is `active`. A candidate flag
does not change a Deployment; it only permits the next isolated experiment
after a Terraform plan/apply receipt proves the candidate was activated.

The matrix defines three independent floor policies:

- `elastic-zero`: zero Ready replicas and zero GPU nodes, including a fully
  preemptible cluster;
- `prepared-node-zero-pod`: a paid node floor with the model at zero, used to
  isolate model startup from provider provisioning; and
- `latency-critical-one`: one Ready replica and node, reported only as a warm
  non-cold reference.

This keeps floor policy separate from model compatibility. Accelerator pool,
GPU SKU, GPU count, capacity type, topology, and MIG geometry come from the
typed compatibility receipt rather than a B300-named branch.

Several preserved canonical Deployment names contain the historical `-b300`
suffix. The framework treats these as opaque route identities, never as a
scheduling selector. A heterogeneous cluster registers each model/pool cell
with its own exact accelerator-pool and compatibility tuple, so H100, H200,
B200/B300, GB-family, or RTX-class experiments can coexist without claiming
that a model supports a device it has not actually passed on.

## Startup phase instrumentation

`model_autoscaling_acceptance.py --semantic-call-count 2
--capture-startup-phases` now measures the required product sequence:

```text
zero -> activation accepted -> scheduled -> image/artifact/runtime phases
     -> Ready -> semantic call 1 -> semantic call 2 -> zero
```

The collector uses Pod/Node conditions, bounded Kubernetes pull events,
init-container status, container start status, and timestamped structured
runtime markers. It stores only allowlisted annotations, image IDs, hashes of
argv/non-secret environment identity, selected Node labels, NodeInfo, and
extended NVIDIA capacity. It never stores a PAT or semantic request/response;
the two results are represented by SHA-256 only.

Evo2 now emits exact JSON markers for artifact localization, weight load, and
first-use engine/compile warmup. GLM remains deliberately fail-closed: until
the pinned vLLM image exposes reviewed timestamped weight-load and compile
markers, its phase receipt lists those events as missing and cannot be used
for promotion. The total T0-to-call measurements remain executable in the
meantime.

Evo2 is the only route whose runtime receives new structured phase markers in
this source change. Every other route remains fail-closed unless its exact
runtime image is independently shown to emit the reviewed marker contract;
the matrix declaration alone is not evidence that a marker exists. The
machine `runtime_marker_contract` therefore denies markers by default,
accepts the six reviewed names only for the Evo source-instrumented path, and
rejects even syntactically valid marker names from the other 15 routes. Evo's
Deployment image still has to be rebuilt and the markers observed before a
live receipt can use them.

The machine Deployment identity contract covers all 16 routes and preserves
the same default denial. Thirteen templates now have all three exact annotations.
Six of the original nine gaps were closed from immutable evidence: MolMIM's
exact `.nemo` SHA-256, ProteinMPNN and RFdiffusion's exact checkpoint SHA-256
values, Qwen's exact 15-file content digest, Boltz2's three-file public model
manifest, and DiffDock's six-file embedded runtime-model manifest. Boltz2 and
DiffDock are the qualified public upstream fallback Deployments; their
canonical NIM cache fields remain unresolved and are not substituted for these
fallback identities. The aggregate algorithm and evidence boundary are
documented in `MODEL_CONTENT_IDENTITY_CLOSURE.md` and validated by the existing
artifact-manifest implementation.

Cosmos3-Nano binds its immutable upstream model manifest and runtime image to
the one-GPU route. Its portable manifest deliberately omits a
GPU-family-specific compile-cache ABI, so compiled-runtime cache reuse remains
blocked until the selected pool supplies and proves the exact ABI. Its inline
weight localizer now supports the rendered shared RWX cache with a single
writer, exact full-hash verification before atomic publication, and a
deterministic warm-hit receipt. This is conventional weight-cache reuse, not a
GPU snapshot or a measured latency claim. It has no new FS2 cold-start
observation in the historical evidence table, so the next admitted work must
still establish the shared-filesystem baseline and replacement-node result.

For `msa-search-pdb70`, `openfold2`, and `openfold3`, the exact runtime image
and compile-cache ABI are recorded, but the model-content annotation remains
deliberately absent. Their catalog artifact contracts still say `unresolved`
with a null manifest digest. These three routes cannot produce an accepted
benchmark attempt until an immutable NIM-cache artifact manifest supplies the
missing exact aggregate digest. No support or latency claim follows from
matrix membership. The recorded
`driver-580.173.02-sm103` ABI describes only these checked-in historical B300
fixtures; a renderer targeting another pool must replace it from that pool's
exact compatibility receipt rather than treating it as a platform default.

Matrix validation resolves every complete model's contained RFC 6901 JSON
pointer and the compile-cache ABI's exact Kubernetes YAML selector. The
resolved scalar values must match the matrix annotations. Before either a
control or candidate attempt, the runner also requires `state=complete` and
binds those annotations to both tuple content digests, the runtime-image
digest, and the compile-cache ABI. Terraform rejects an enabled attempt for a
blocked row; its default disabled mode remains an inert matrix inspection path.

The phase observation schema permits `missing` only in raw observations. The
closed post-acceptance promotion receipt still permits only `observed` or
explicit `not-applicable`; therefore missing instrumentation cannot be
laundered into a successful promotion.

## Exact identity and snapshot gate

Every attempt requires the existing closed compatibility tuple, including the
exact model/tokenizer/oracle/request digests, runtime source/image/argv/env,
loader format, CPU/OS/kernel/container runtime, accelerator pool, GPU vendor,
product/chip/compute capability/memory/count/topology, driver/CUDA, artifact,
storage/PVC, compile ABI, checkpoint tool, CRIU, and capacity state.

The live observation additionally binds the actual Pod image IDs, runtime
argv/environment hashes, Node UID, kernel, container runtime, and allowlisted
accelerator labels. A mismatch fails the attempt before a receipt is accepted.
The argv digest is canonical JSON over primary-container name, command, and
args in Pod-spec order. The environment digest is canonical JSON over env/envFrom
order, SHA-256 of literal values, and exact ConfigMap/Secret/field/resource
references; it never records the literal values themselves. Compatibility
tuple generators must use that same collector representation.

`cold_start_framework.py snapshot-eligibility` denies by default. It permits
only an isolated experiment when donor, target, and a current PASS
qualification agree. It always returns `production_promotion: denied`.

Full GPU and MIG are separate contracts:

| Partition | Required identity |
| --- | --- |
| Full GPU | `mig_mode=disabled`, `nvidia.com/gpu`, donor/target GPU UUIDs, topology receipt, donor/target Node and PVC identity digests |
| MIG | `mig_mode=enabled`, exact live-discovered extended resource name and profile, donor/target MIG UUIDs, GPU-instance IDs, compute-instance IDs, and donor/target Node/PVC identity digests |

Cross-partition, cross-profile, cross-chip, cross-driver, cross-runtime, and
cross-topology restore remain denied. MIG fit and CUDA success are independent
gates; a model fitting a slice does not make a full-GPU snapshot portable to
that slice.

## Terraform-owned attempt sequence

The `models/cold-start` benchmark package owns no cluster
resources. Its default `run_attempt=false` performs no live action. When
enabled, one `terraform_data` instance runs exactly one immutable attempt.
Odd ordinals must be the conventional control; even ordinals must be the
candidate. Every attempt after the first must chain the previous receipt
digest. This makes the exploratory sequence exactly:

```text
control-1, candidate-1, control-2, candidate-2, control-3, candidate-3
```

The Terraform state contains only non-secret target and digest identities.
Before `terraform apply`, export local mode-0600 input paths; do not put these
paths or their contents in `.tfvars`:

```bash
export FS2_COLD_START_TOKEN_FILE=/absolute/private/operator.pat
export FS2_COLD_START_REQUEST_DIR=/absolute/private/semantic-requests
export FS2_COLD_START_IDENTITY_DIR=/absolute/private/compatibility-tuples
```

Candidate attempts also require:

```bash
export FS2_COLD_START_MECHANISM_ACTIVATION_RECEIPT=/absolute/private/activation.json
```

Snapshot candidates additionally require
`FS2_COLD_START_SNAPSHOT_ELIGIBILITY`. All files are owner-only mode 0600.
The activation receipt validates against
`cold-start-mechanism-activation.schema.json` and binds the experiment, model,
mechanism, source commit, compatibility tuple, saved Terraform plan hash, and
applied workload contract.

Run `terraform plan` and inspect the exact `terraform_data` replacement before
each attempt. The benchmark runner independently denies both the retained
cluster and the prohibited cluster, requires the exact run-root kubeconfig and
`fs2-disposable-<run_id>` context, writes mode-0600 receipts under the run root,
and suppresses child stdout/stderr so payloads cannot enter Terraform logs.

## Rollback and failed attempts

The benchmark root does not own or mutate the candidate Deployment, cache, or
node pool. If an attempt fails, retain its mode-0600 failure receipt, stop the
chain, and use the disposable lifecycle's reviewed workload plan to restore
the exact conventional manifest/image/artifact tuple. Verify one canonical
semantic call and the selected floor, then return the Deployment and GPU pool
to zero. Do not treat destroying the `terraform_data` benchmark state as a
workload rollback, and never reuse a failed snapshot or cache artifact in a
later chain.

## Next live slice and prerequisites

Use a fresh disposable Terraform lifecycle after the current acceptance run is
torn down. Do not reuse or replan that active state. The live prerequisites
are:

1. deploy the final source commit and rebuild the Evo image containing the new
   phase markers;
2. pass all 16 canonical HTTP and MCP semantics;
3. configure KEDA with the selected model at a zero floor and prove the GPU
   pool can return to zero;
4. generate exact compatibility tuples from in-container CUDA, driver,
   topology, allocated-device, artifact, and storage receipts, and render the
   matching model-content, runtime-image, and compile-cache ABI annotations on
   the benchmark Pod template;
5. materialize owner-only canonical semantic request files;
6. run the conventional attempt first;
7. for Evo, add an exact Hugging Face two-part acquisition manifest and
   content-addressed SFS-to-fresh-PVC copy path—do not reuse the incompatible
   NIM artifact plan;
8. for GLM, use the existing catalog-rendered read-only SFS artifact path and
   the same pinned vLLM image/args, plus timestamped vLLM phase markers;
9. apply only a reviewed candidate plan and emit its activation receipt; and
10. alternate six attempts, then return the candidate and cluster to zero.

Shared cache and local NVMe candidates must also run a preemption-replacement
cell. ImageVolume/modelcar candidates require exact registry digests and
runtime-path parity. Snapshot candidates remain blocked from production by
the separate artifact-custody task even if an isolated experiment passes.

No live cluster, cloud resource, Terraform state, registry, endpoint, route,
or Secret was changed while implementing this framework.
