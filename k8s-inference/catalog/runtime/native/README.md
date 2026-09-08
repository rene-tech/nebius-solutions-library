# Native model declarations

These additive declarations reuse the existing model record, exact variant,
artifact manifest, semantic contract and selected deployment-runtime machinery.
They do not amend the archived 16-model catalog digest, grant a static route,
or turn a measured worker into an unmeasured platform qualification.

Each `*.json` uses `fs2-serve.nebius.ai/native-catalog-model/v1`:

- `record`: the shared deployment-runtime model shape, with the exact source,
  immutable image, resource requirements, native HTTP operation and evidence.
- `variant_id` and `runtime_architecture`: distinct runtime identity; currently
  the qualified aging variants are `altumage-cuda-v1` and
  `phenoage-clinical-cpu-v1`.
- `artifact_manifest`: a relative path and canonical SHA-256. File sizes and
  hashes describe image-owned weights/preprocessing or formula source bytes.
- `semantic_requests`: two distinct original request identities, serialization
  and invocation contract. Aging's retained request bytes use compact JSON in
  insertion order, without a newline; this is not the sorted canonical format.

`fs2_serve.native_catalog.augment_native_catalog` validates these declarations
only after the original archive/bindings are validated. Each native record and
contract receives its own content identity. The selected
`../deployment-runtimes/*` entry and live ModelDeployment publication still own
the active route. An installed candidate alone is not an invocation grant.

## Aging qualification boundary

`phenoage` is the published rounded-coefficient clinical formula using age and
nine blood biomarkers. It has no weights or GPU request. Its formula manifest
binds the actual 2,298-byte runtime source under the implementation repository's
Apache-2.0 license; the upstream paper is cited in the model documentation.

`altumage` consumes 20,318 normalized methylation CpGs. Its manifest binds the
three actual serving files (weights, robust-scaler arrays and CpG names),
3,567,654 bytes total, with pinned upstream MIT source. This CUDA image is
qualified on H100 SM90 only. B300 remains unverified; a CPU calculation in the
same CUDA image is not a separately qualified CPU-only image.

The direct receipts are [PhenoAge](../../../acceptance/aging-20260908/phenoage-r01.json)
and [AltumAge](../../../acceptance/aging-20260908/altumage-r01.json).
They establish two native semantic outputs and runtime identity, not public
HTTP/MCP, App replica changes, public cold starts or scale-to-zero. Accordingly,
the selected entries initially keep those platform qualification flags false.
Neither enables GPU snapshots. All fixtures are synthetic research/demo inputs;
the two clocks have different targets and are not diagnostic recommendations.

## Extending the same path

Add a native declaration and exact artifact/source mirrors, a selected runtime
entry, profile/template metadata and only genuinely measured hardware bindings.
The same shared loader and controller handle additional models; do not introduce
a second catalog service, fabricate an archived B300 record, or reuse another
model's qualification hashes. CPU capacity comes from the declared general CPU
pool and the full Pod CPU/RAM request, not from a synthetic accelerator token.

The model-local HTTP package and input documentation are in
[`models/aging`](../../../models/aging/README.md). Root-owned deployment and
separate public App acceptance follow offline contract validation.
