# Protenix v2 adapter

`protenix-v2` preserves the upstream v2 identity and its 2,560-token bound. Its
canonical workload profile puts offline CPU input conversion in `prepare-data`;
only its content-bound relocatable handoff unlocks the GPU `sample-structure`
stage. The first lane admits `msa_mode=none` only: remote, precomputed, template,
and RNA-MSA paths remain closed until every referenced file has a reviewed
localization contract. Seeds are bounded typed integers. Runtime commands,
shell text, storage URLs, and mount paths do not enter the public request.

The exact `protenix-v2.pt` object is pinned to the immutable
`TMF001/protenix-v2-weights@653edab2…` mirror revision: 1,859,785,497 bytes,
SHA-256 `8f931f97…d599`, and MD5 `49016ebf…673e`. It was loaded offline as 4,174
float32 tensors and 464,442,431 elements. This is a verified third-party mirror,
not a claim of publisher-byte parity and never a v1 substitute.

The lane requires one `protenix-v2` artifact rooted at `/models/protenix-v2`:
the exact checkpoint, all four mandatory common files, one composite manifest,
and one localization-ready marker. Both CPU and GPU stages consume the same
read-only bundle. The localized-tree content digest is `5e1c3b54…4d48`; the
canonical manifest and ready-marker digest is `a093d28e…c6b7`. Runtime checks
consume that small marker instead of hashing the 1.86 GB checkpoint per
attempt. The route remains fail-closed until the
controller verifies this localization before GPU admission and real H100
semantics pass.

GPU sampling also binds a deployment-configured writable cache PVC at
`/cache/protenix`; it does not alter the immutable model bundle. The renderer
owns the exact cache variables: `TRITON_CACHE_DIR=/cache/protenix/triton`,
`CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton`,
`TORCH_EXTENSIONS_DIR=/cache/protenix/torch-extensions`, and
`XDG_CACHE_HOME=/cache/protenix/xdg`. This persistent compiler/JIT cache is an
auxiliary L1+ optimization, not L2. The numbered ceiling is `L1`, the qualified
level remains `Off`, and the route remains false until separate first-run and
warm-cache H100 evidence establishes truthful readiness. L2 requires an actual
GPU/process snapshot on shared storage.
