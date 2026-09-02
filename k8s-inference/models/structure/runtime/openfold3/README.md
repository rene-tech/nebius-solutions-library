# OpenFold3 adapter

`openfold3` is the open AQLab implementation and checkpoint. Its ID, source,
validator, and canonical profile are distinct from native `alphafold3`.
Successful OpenFold3 execution cannot satisfy a native AlphaFold 3 request or
readiness gate.

CPU preparation owns input translation and local MSA/template work, committing
logical `prepared-input` and `prepared-manifest` artifacts before the GPU stage.
Public requests contain no remote MSA service, command, storage URL, or mount
path. This open-runtime package is not the existing NVIDIA NIM route, and it
rejects unsupported covalent chemistry instead of silently dropping it.
