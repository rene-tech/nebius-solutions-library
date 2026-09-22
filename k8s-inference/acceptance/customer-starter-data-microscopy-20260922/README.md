# Microscopy starter-data extension — 22 September 2026

Status: deployed and backfilled. Starter pack v2 adds ten real fluorescence
microscopy fields for cell counting and instance separation without changing or
overwriting `examples/v1/`.

## Content

The cases `imaging/u2os-nuclei-01` through
`imaging/u2os-nuclei-10` come from BBBC039v1. They cover its official
training, validation and test partitions and span 6–161 annotated nuclei. Every
case contains:

- a metadata-free, contrast-normalized PNG at the original 520×696 geometry;
- the official annotation decoded to a 16-bit contiguous instance-label PNG;
- a JSON reference count and per-instance pixel areas;
- a Cellpose CPSAM v2 recipe for cell counting/segmentation; and
- a SAM 2.1 automatic-image recipe for general instance separation.

The selected source archive checksums, source filename, partition, license,
normalization and mask-decoding transformations are pinned in the pack
manifest. BBBC039v1 is CC0. It contains a cultured U2OS osteosarcoma cell line,
not patient tissue, and this starter data is not clinical-validation evidence.

## Qualification

All 143 v2 recipes have exact recipe/input-hash-bound proofs: 123 unchanged
proofs inherited from immutable v1 inputs plus 20 new public-endpoint runs.
All ten Cellpose and all ten SAM 2 runs succeeded and their downloaded masks
passed geometry, integer-label, nonempty-object and artifact checksum checks.
See [the payload-free per-image results](model-validation.json).

Observed post-repair server processing was 1.834–9.168 seconds for Cellpose and
3.29–10.333 seconds for SAM 2. These are one warm cohort, not latency promises.
Cellpose predictions differ from the public reference counts, especially for
the densest fields; that is useful demo/evaluation behavior, not hidden as a
perfect-accuracy claim. SAM 2 is capped at 128 proposals and therefore is not
presented as the exact counter for dense images.

The first Cellpose operation exposed a live deployment-label drift: its ready
Pod did not have `app.kubernetes.io/component=model-runtime` and
`app.kubernetes.io/part-of=fs2-serve`, so the gateway NetworkPolicy correctly
blocked it. The repository manifest and regression test already require both
labels. The live template was reconciled, an orphaned Pod on a dead node was
removed, gateway-to-runtime readiness returned HTTP 200, and a fresh
post-repair operation completed in 3.724 seconds. The outage-inflated receipt
is retained privately but is not used for the release timing table.

## Release and rollout

- Pack manifest:
  `9387cfd01e71fdfce67e08008ebfccd359082e60485ecf30b4f17a30d4df22b6`.
- OCI index:
  `sha256:85f93b8335f088ec8637e73e8811874184e622fb2b66542c3e8ee12fc8ec5407`.
- The data-only image has embedded SPDX and SLSA attestations. Its Trivy scan
  returned no package, secret or misconfiguration result entries.
- Helm revision 212 changed only the starter-pack init image and manifest
  checksum from revision 211. The three gateway replicas rolled without an
  availability loss.
- All 12 currently eligible distinct workspace/demo buckets completed v2 on
  attempt 1; there are zero incomplete v2 receipts. Disabled storage remains
  excluded.
- A customer-key-disclosed, bucket-scoped credential independently downloaded
  and checksum-verified all 51 BBBC039-specific objects from one active demo
  bucket under `examples/v2/`. No credential material was retained.

Exact release pins are in
[`starter-data/release-v2.json`](../../starter-data/release-v2.json). Raw model
artifacts, customer credentials, bucket names and full operator receipts remain
outside Git under the protected handoff directory.
