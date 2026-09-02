# Protenix v2 adapter

`protenix-v2` preserves the upstream v2 identity and its 2,560-token bound. Its
canonical workload profile puts CPU input conversion, MSA, and templates in
`prepare-data`; only a committed enriched input and manifest unlock the GPU
`sample-structure` shards. Seeds are bounded typed integers. Runtime commands,
shell text, storage URLs, and mount paths do not enter the public request.

The adapter remains fail-closed with `ProtenixV2ArtifactUnavailable`. As of
2026-09-02 the official v2 checkpoint is not publicly retrievable; no v1 or
third-party weight can satisfy this model ID. Fixtures prove contract and
semantic-validator behavior only, not model readiness.
