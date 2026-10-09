# Retain the deployed admin UI in root inputs

Read-only verification, 5 October 2026, 18:38 UTC. No private tfvars, state,
cluster or registry content was changed. This is a narrow input correction for
parent review, not a Terraform apply or a whole-root replay-safety verdict.

The authoritative H100 root input still pinned the September admin repository
`fs2-platform/fs2-serve-admin-console`, digest `6428b350…`, source `b33d7314…`
and its old provenance. The live Deployment is generation43 with two Ready
replicas from **`fs2-platform/admin-console`**, digest `de4df56d…`.

Replace only the existing `deployment.applications.admin_console` block with:

```hcl
admin_console = {
  repository = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/admin-console"
  digest     = "sha256:de4df56d87fe141bdc456cc6bee8309e6a2abb21cdc92e13cba3400a3438366f"
  provenance = {
    source_commit = "f9e1f66664e3f99352c85df5384ac4c73b09a468"
    source_tree   = "14a234204b9a0136eb5313914878e8cbc1a995d4"
    sbom_sha256   = "c7463e5630d5423d710572c73964cd3d740ca4c885703ea0b59acbcf7edf48a7"
    sbom_format   = "cyclonedx-json"
  }
  replica_count = 2
}
```

This snippet is not a standalone complete tfvars file. Do not replace the entire
`deployment` or `applications` object with it. Replica count remains two; no API,
model, GPU pool, route, key or bucket setting is included in this correction.

## Provenance verified, not inferred

- Kubernetes main image and source/tree/SBOM annotations identify the same
  release as the retained October2 customer-workbench release record.
- `crane digest` confirms the immutable live index `de4df56d…`; `crane config`
  source labels match the full commit and tree above.
- The image index contains the Linux/amd64 manifest
  `70634dc8879fd4c3a1fa9c3289b4de114b3a4188fef01aba2b35b9a351e53bd4` and
  attestation manifest
  `0405db0c02a32134e3e392468b3cd13f176c0ef80a07acbe7b2d8af2c9dbe48c`.
- The SLSA v1 blob
  `9d2530c10fe1d988d07921d5f90f0b6cc7ffd67a896caa5faf650d7a464be7a0`
  names that exact platform manifest and its build arguments reproduce the
  source commit/tree and package-lock SHA-256
  `1d45f7fa8912c7801a02d02ecfb75c6c69281ac1ce6ea5135851250a547eda45`.
- `git rev-parse f9e1f666…:k8s-inference/components/admin-console` resolves
  exactly `14a234204b9a0136eb5313914878e8cbc1a995d4`. This is the **admin
  component tree**, not the whole repository tree. The commit's package-lock
  bytes also hash to the attested value above.
- The actual retained file
  `/home/tux/secure-handoff/fs2-workbench-state-20261002/admin-r4-sbom.cdx.json`
  hashes to `c7463e5630…`, is CycloneDX1.6 with1022 components, and identifies
  the exact live repository/index digest. Its matching build receipt is
  `admin-r4-build.json` in that directory. This is not a new SBOM generated for
  a merely similar image. The registry's separate SPDX attestation is not the
  CycloneDX checksum supplied to Terraform.

Whitelisted exact before/after fields, full hashes and verification identities:
`/home/tux/secure-handoff/fs2-lynx-longrun-20261005/admin-console-input-review.json`.
The reviewed root-file SHA-256 was
`d81e60be0c9cd36515338266f9a3d62667e4969723d1bdfb3d842e7eba443a9c`.
Re-read the block before editing if the parent has since updated other release
inputs; do not replay a complete older file.

Parent applied exactly this nested-block correction at 18:41 UTC after a fresh
read of live generation43 confirmed the same image and two Ready replicas.
Terraform formatting passed. No apply, state import, replica or live UI change
was needed: this records the already-deployed release in the authoritative
private root input. The API191e, zero hot floors and managed RDMA declarations
were preserved.

## Explicit persistence boundary

Parent has separately refreshed the API image, zero hot defaults and approved
full-H100 RDMA declarations. Those changes must be retained. Updating this admin
block prevents the specific normal-input rollback to the older UI; it does not
claim that every root/stage input is reconciled.

The eight retained App registrations and existing H200 group still have the
separate ownership/input residual documented in [PERSISTENCE.md](PERSISTENCE.md).
The current live admin-owned zero floors and exact controller bundle bodies stay
valid; no new static writer is proposed. H200 import or capacity change is not
part of this correction, and this residual does not block the currently
deployed GROMACS path. Regenerate stale September stage inputs only as part of
an explicitly reviewed future complete root render/plan, not by replaying them.
