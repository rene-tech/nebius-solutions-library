# OpenFold3 adapter

`openfold3` is the open AQLab implementation and checkpoint. Its ID, source,
validator, and canonical profile are distinct from native `alphafold3`.
Successful OpenFold3 execution cannot satisfy a native AlphaFold 3 request or
readiness gate.

CPU preparation validates the MSA-free input and commits a content-bound query,
provenance marker, and request-local runner configuration before the GPU stage.
The reviewed non-secret v0.5.0 base runner YAML is baked into the image, copied
to writable request storage, and patched with the exact requested seed list.
The OpenBind invocation binds that runner, the one exact checkpoint, and the
separate exact `components.bcif`; it always forces MSA-server and template
lookup off. Precomputed MSA input remains unsupported in this first lane.
Public requests contain no remote MSA service, command, storage URL, or mount
path. This open-runtime package is not the existing NVIDIA NIM route, and it
rejects unsupported covalent chemistry instead of silently dropping it.

Only the GPU `inference` stage receives the same-namespace operator PVC
`scientific-openfold3-cache`, mounted writable at `/cache/openfold3`. Its
deployment-owned environment fixes Triton, Torch extension, and XDG paths to
`/cache/openfold3/triton`, `/cache/openfold3/torch-extensions`, and
`/cache/openfold3/xdg`. Cache mounts have no logical artifact ID and cannot
impersonate the checkpoint or CCD artifacts; requests and adapter output do not
carry a PVC name or cache path. This is an auxiliary L1+ compiler/JIT cache,
not L2. The numbered ceiling is `L1`, the qualified level remains `Off`, and
the route stays disabled until paired first-compile and warm-compile H100
evidence exists. L2 requires an actual GPU/process snapshot on shared storage;
no L2/L3/L4 snapshot or residency claim is made.
