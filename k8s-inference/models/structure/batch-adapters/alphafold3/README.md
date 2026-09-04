# AlphaFold 3 scientific batch adapter

This adapter compiles an artifact-service-verified scientific manifest holding
one raw AlphaFold 3 JSON entry into the two stages understood by the current
scheduler:

1. `data-pipeline` uses 16 CPU, 64 GiB memory, and 32 GiB ephemeral storage in
   `fs2-academic-poc` through `academic-scientific-cpu` / `reference-data-cpu`.
   It mounts the complete public reference root read-only at `/reference-data`
   with no Kubernetes `subPath`. The runtime verifies the producer-generated
   terminal receipt, its `dataset_sub_path`, the in-tree
   `.fs2-manifest-sha256` marker, and the sibling `manifests/sha256` document.
   After a terminal runtime PASS, the adapter collector verifies every indexed
   handoff file by path, size, and SHA-256, rejects symlinks and traversal, and
   writes a deterministic bounded tar artifact for the next stage. Payloads
   plus the index are capped at 255 MiB; deterministic tar framing gets the
   remaining 1 MiB of the companion materializer's 256 MiB compressed and
   expanded limit. The collector therefore holds at most about 511 MiB while
   validating members and constructing the tar, within its 2 GiB sidecar.
2. `inference` consumes the relocatable tar handoff and one H100 from the
   deployment-resolved `academic-scientific` GPU lane. It mounts only the
   private `af3.bin.zst` object from `academic-assets-runtime-rwx`; it never
   mounts the reference databases. Its collector validates the terminal runtime
   receipt, exact private parameter identity, top-ranked mmCIF, and confidence
   summary before either result is committed.

The adapter invokes the immutable r6 runtime by its published command/IO
contract. It does not emit the retired `fs2-run-alphafold3`, `/databases`,
`--input-json`, `--processed-json`, or `--handoff-tar` surface.

Academic authorization is deployment-bound. The public request has no license
receipt, token, credential, path, or URI. The profile must project the live
`Granted` / `Authorized` state as `academic` + `verified`; removing that
operator-owned state makes compilation fail closed.

The route remains closed. The r6 image has real H100 semantic evidence, but it
predates atomic receipt publication and the aligned 256 MiB handoff contract;
it is historical evidence, not a production-protocol-compatible image. A new
immutable successor must be built, inspected, and H100-qualified from this
source before activation. The
candidate profile intentionally contains no invented public-reference digest;
promotion must add the producer-generated terminal receipt and its exact tree,
manifest, and inventory identities to the trusted execution map. The exact
mount, scheduler, image, and gate identities are in `contract.json`.
