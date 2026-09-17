# CP/admin image publication — 17 September 2026

Published candidate images, **not a deployment or customer acceptance receipt**.
No cluster mutation was performed by this build lane. Both targets were absent
before publication; no existing tag was overwritten. The authorized `sandbox2`
profile was used explicitly with a private Docker configuration. Its temporary
registry login was removed afterward, and the default CLI profile was unchanged.

Exact committed source: `b33d73149d5f8be96cac70711b17d039927cea2a`.
Git tree: `30a1b9c26f61322e9f3e2e5740f6ac29e4362425`.
Platform: `linux/amd64`. Tag: `cosmos-stockholm-b33d73149-20260917`.
Repository prefix: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/`.

| Repository | Published OCI index digest | Linux/amd64 runtime manifest |
| --- | --- | --- |
| `fs2-serve-control-plane` | `sha256:1cf5df3c2a7b11207eed3f939cb3c2b12b2c5920e3b67a52da098104bb7ab8df` | `sha256:f66c9e1ca7daff45c37a74888410b6e6c3d8cf5ebf354776a3b9b2342c66e965` |
| `fs2-serve-admin-console` | `sha256:6428b3500d2dd6784c0ff2308335b3d2962f725434cc5e7ea8fd5098dbf21659` | `sha256:9194ad5171c1c71467845fd0311caaad2462a6eefa86886db8a844e6dae1551c` |

The indexes retain max-mode SLSA provenance and SPDX SBOM attestation manifests:
CP `sha256:1625a468cd08068fac7c575f6066e14abfb9bc171d3790830a881b68441828da`,
admin `sha256:8fb66d74cc4f4642113c3d8d53b1b064e8d8c37779ac451f6ec6dbd96452591e`.
OCI archives were copied with all manifests and digest preservation. Remote
index descriptors and runtime labels were verified against local receipts.

## Required admin chart provenance

```yaml
sourceCommit: b33d73149d5f8be96cac70711b17d039927cea2a
sourceTree: 30a1b9c26f61322e9f3e2e5740f6ac29e4362425
sbomSha256: c9f1fa62d385161f28c1d30a46ba5d2af933bba4886da4684816b812cec04c1b
sbomFormat: cyclonedx-json
```

The separate CycloneDX 1.6 document was produced with Syft 1.43.0 against the
published immutable admin runtime manifest and contains 1,022 components. This
is an inventory, not a vulnerability-free assertion. The first local Syft scan
rejected BuildKit's nested OCI index; the retry scanned the exact published
runtime manifest successfully, with no rebuild, substituted image or hidden
failure.

## Build inputs and verification

| Input | SHA-256 |
| --- | --- |
| CP `uv.lock` | `17eeae7ba86ca9514c867e41212b42fad623bf52440abcfbc047cb9fe0b6fcae` |
| CP Dockerfile | `51f1b1430188e4a1e6cb16828405246a0687a07232bcf2057c75ddee52619044` |
| CP context policy | `efdc212a34137571d26e32344513c7218434be7629e1ede8a48d26fea412a175` |
| Admin `package-lock.json` | `1d45f7fa8912c7801a02d02ecfb75c6c69281ac1ce6ea5135851250a547eda45` |
| Admin Dockerfile | `9459768101d0885c056b786cae86036369165b8faee40ac5d487a091b7153914` |
| Admin context policy | `fa9b5bd8b9b14f4ea418446f4cac952da0f7e1437d90096dc35206c8b50a65f4` |

CP used the repository `scripts/build_image.py build` exact-commit wrapper:
426 filtered context files matched its committed allowlist, frozen dependencies
and wheel installation passed, packaged migration count matched
`EXPECTED_MIGRATIONS`, catalog/native checks passed, collector UID readability
passed and the executable help check passed. Admin used the same committed
Git archive, its closed Dockerfile context policy and frozen npm lock; production
TypeScript/Vite build and Dockerfile artifact checks passed. Both builds retained
digest-pinned base images and the pinned BuildKit SBOM scanner.

Protected operator receipt directory:
`/home/tux/secure-handoff/cosmos-stockholm-build-20260917-z7qpSC`.
No credential is included in the sanitized receipt or these docs.

| Retained receipt | SHA-256 |
| --- | --- |
| `build-publication-receipt.json` | `f1ede31db2917bfdcbda09c6b601cc2bb389171f42cb7fe12e67e2089bc3d792` |
| `cp-provenance.json` | `24b780e375bc6b241963e74f9000762e5b99b4189a4e1172d242d9bac54fd911` |
| `admin-build-metadata.json` | `27fe3e88940574560aa286f67b665f35a0bf738d228f2400c3f16d68ece9203b` |
| `cp.oci.tar` | `377772f008e55c107838ec6a985f8068b9b3ef3978ac2fb04da54fdede0ed399` |
| `admin.oci.tar` | `bbb6bd769a976ed3a3a6bd252339f6f789e0d1f4a743e157d4a1c12789744b1d` |

The release owner still controls coordinated deployment, database migration,
historical usage reconciliation and exact customer/GPU acceptance. Clear or
replace the recorded qualification map on release-identity changes as described
in [the qualification contract](../acceptance/customer-readiness/README.md).

## CP-only metrics correction publication

The initial rollout exposed a runtime-role SELECT grant mismatch in the new
customer-operation metrics query. Correction source
`d586613f160b58fa1f87f55f8fa997ce5d7faf86`, tree
`78c30d40b09ece083c45ea577fc6ed910c82ac36`, was built through the same exact-source
wrapper and published under tag `cosmos-stockholm-d586613f1-20260917`.
The tag was absent before build and immediately before publication. Admin remains
the original immutable `6428b350...` image above; no admin rebuild was needed.

- CP OCI index: `sha256:f96d890a19fb917322f5986d1ee81b1d5911643e35b62d76a46503c5f35654f8`.
- Linux/amd64 runtime: `sha256:8f6154b69c4e5c3d2eaa765aee093f9fc5ed9bbffe6c9d50d8eab2e771c6f2f7`.
- Attestation manifest: `sha256:3667675e4a8851ebd0acd7f85996470d960cbfe0c84515d398e06f696e8bfa32`.
- CP provenance JSON SHA-256: `a5c7e078ca10376b3768666ed1ed77ab1e7a7f06c041e0b7d790505eaf2f52b8`.
- OCI archive SHA-256: `33413be19cac590ad4ecb67cf1fa210229ccb5459e14d19410375e5fc0c22f06`.
- Publication receipt SHA-256: `95bee7893c1ab551fd9311fbac034643785578cc80158f516317ab92e64d7837`.

Protected correction receipts:
`/home/tux/secure-handoff/cosmos-stockholm-metrics-build-20260917-YZMLav`.
All 426 filtered context files matched committed source; packaging and image
checks passed, remote labels/descriptors matched, and max-mode SLSA/SPDX
attestations were retained. CP lock/Dockerfile/context-policy hashes are unchanged
from the table above. Private registry login was removed; default CLI profile
was unchanged. This receipt proves publication, not corrected live metrics or
customer acceptance. Migration32 and its release-contract hashes are unchanged.

## Release-142 deployment verification scope

The 2026-09-17 14:29:32 UTC readback confirmed the corrected `f96d890a...` CP
image on three Ready replicas, the unchanged `6428b350...` admin image Ready 2/2,
and model controller Ready 2/2. CP/controller use
`fs2-cosmos-envelope-608b68ad9c0f32b2` and
`fs2-cosmos-bundles-d556dcfd58d35b29`; CP mounts preserve all 20 model identities,
23 template revisions and five voice qualifications. See the
[contract handoff](../acceptance/cosmos-stockholm-deployment-20260917/CONTRACT-HANDOFF.md)
for source pins, unchanged sibling guarantees and canonical Terraform boundary.

All three CP `/metrics` responses were 200; actual Prometheus targets scraped
166 customer-operation and 155 semantic-exchange series per pod. All 13 rules
were loaded and evaluated without errors. Existing lifecycle/certificate alerts
remained active and GPU-observer readiness was only 13/15 (15/15 updated), so
neither full telemetry coverage nor overall customer readiness is claimed.
The root-owned public readback covered nine HTTP-200 surfaces at 14:28:09 UTC.
No inference, new credentials or deployment mutation was performed by this lane.

Private receipt:
`/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/final-deployment-verification-r142.json`,
SHA-256 `32fde8d007eeaba2982aa330e4eae5785d7347c6be3b6056d237f534aad29692`.
It records the expected final Helm history 142, not an independently completed
Helm release or customer acceptance. Final public sibling discovery and Cosmos
inference qualification are separate acceptance receipts.

## CP-only native media correction publication

Real release-142 public testing exposed valid Cosmos MP4 responses being rejected
by a JSON-only gateway decode step. Correction source
`c7f99b46fcd22f8ee3d63b8603715eb52a58ac4c`, tree
`1a95f16f81706a57584ca3ed8e92c983f397ce84`, was built from the exact committed
426-file context under tag `cosmos-stockholm-c7f99b46f-20260917`. Frozen inputs,
packaging checks and remote manifest/label verification passed. No admin rebuild
or schema change was needed.

- CP OCI index: `sha256:624763c6141a990125c37b71fccc0a8d7ba5371a4efd5be325336cbc58d54020`.
- Linux/amd64 runtime: `sha256:7007498210f937f39a9032dd571bf90bab7cf5c6feec5cad6a89d6ac92be8d69`.
- SLSA/SPDX attestation manifest: `sha256:85ab78bae8b2794fb43b2aed3ec960433b6f5f4cb8064de8178865f4efd7ea75`.
- Publication receipt SHA-256: `489e58a98631a82e8dc1fca1ef0a3197e30f0bd7c8a2dc5ba3d7d61f3058e3d0`.
- Provenance JSON SHA-256: `9945eefe29f087d475a9fa3d7f6d1c961f2a217a06f045873a6104c338d967d5`.

Protected receipt directory:
`/home/tux/secure-handoff/cosmos-native-binary-build-20260917-gIDbcy`.
The temporary registry login was removed. Publication is separate from public
workflow qualification. Helm 143 deployed this index to the existing release;
CP 3/3 and controller 2/2 were Ready at 14:57 UTC. All nine public/operator read
surfaces returned HTTP 200 at 14:57:48 UTC, with 34 Apps still listed. Public
inference acceptance is recorded separately, including every failed attempt.
