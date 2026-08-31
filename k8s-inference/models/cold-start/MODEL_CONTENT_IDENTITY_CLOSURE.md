# Model-content identity closure

This source-only audit closes the content identity of the two public upstream
fallback Deployments and leaves the three exact NVIDIA NIM Deployments denied.
It does not change the unresolved NIM artifact fields in the canonical catalog,
promote a route, or claim a new hardware qualification.

## Canonical aggregate algorithm

The aggregate is the existing `artifact-manifest/v1` content digest, not a
concatenation of component hashes. The file inventory is sorted by canonical
POSIX path. Each entry is the object
`{"path": <path>, "bytes": <positive integer>, "sha256": <lowercase SHA-256>}`.
The digest is SHA-256 over UTF-8 JSON with keys sorted, no insignificant
whitespace, `ensure_ascii=true`, and one trailing newline. The catalog's
`fs2_serve_catalog.artifacts.load_artifact_manifest` independently reopens and
validates the ordering, byte total, component digests, and aggregate.

## Resolved Deployments

`boltz2` is the checked-in public-upstream Blackwell fallback, not the
unresolved Boltz2 NIM cache. Its three runtime inputs are pinned by
`models/bionemo/boltz2/Dockerfile`, reopened by `server.py`, and observed in
`live-evidence.json`. The immutable Hugging Face revision
`6fdef46d763fee7fbb83ca5501ccceff43b85607` reports the same three LFS SHA-256
values and exact sizes through the public, credential-free revision API. The
canonical three-entry manifest is
`models/bionemo/boltz2/artifact-manifest.json`; its content digest is
`9459dd0c80992f21d07e70ae7d54c318e66a9d5202d6e849a134957b0740d82a`.

`diffdock` is the checked-in upstream fallback image. Git commit
`ff4ae78f51b3e691cd79a35d832b07218b2edcb1` records the immutable acquisition
inventory in
the checked-in DiffDock workload manifest and runtime adapter. The public
solution does not ship an environment-specific acquisition receipt. Any
future content-identity promotion must be recorded through the catalog's
public artifact contract rather than relying on a path from another source
tree.

## Still denied

`msa-search-pdb70`, `openfold2`, and `openfold3` remain blocked. Checked-in
evidence binds their exact NIM image and semantic outcomes, and historical
receipts bind snapshot/checkpoint objects or coarse cache byte counts. None is
an immutable, path-and-file-digest model/NIM-cache manifest for the current
Deployment. Read-only `nvcr.io` manifest requests returned 403 in this audit;
even an OCI image manifest would bind runtime layers rather than independently
identify a mutable NIM cache. Their catalog artifact states therefore remain
`unresolved` with null manifest digests, and the cold-start acceptance contract
continues to deny attempts for these three models.
