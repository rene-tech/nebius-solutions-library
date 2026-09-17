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

## CP-only typed text-to-image default correction

Release-143 public typed T2I operation
`82a3903b-2e4f-4670-8bed-5159d4e8aff9` reached the adapter but failed HTTP 422:
the CP injected `output_delivery=inline-base64`, which the pinned
`TextToImageRequest` does not accept. No model generation occurred. Correction
`f8cbadb3bf14ad5ec157cf8f41466d95de645053`, tree
`80bd96e34b9c076ae5a5d9160d1d442d4705a680`, omits only that specialized T2I
default. `output_format=png` and the legacy JSON image response remain unchanged;
all four typed video modes retain artifact delivery. No adapter, template,
model/runtime image, schema or admin change was made.

The new regression first reproduced the exact T2I `extra_forbidden` failure
against the actual YAML adapter `GenerateRequest`; the other four typed modes
passed. With the correction, **177 focused tests passed**, including all five
actual adapter DTOs, real MCP HTTP/ASGI T2I/V2V admission, legacy JSON/artifact
handling and Magpie regressions. Ruff and whitespace checks passed. Existing
Starlette deprecation and old pytest temporary-directory ownership warnings
remain disclosed; they are not GPU/model acceptance evidence.

Published Linux/amd64 tag: `cosmos-stockholm-f8cbadb3b-20260917`, in the same
`fs2-serve-control-plane` registry repository above. The exact-source wrapper
verified all 426 context files, frozen dependencies, packaging and image checks.
Remote labels/index descriptors matched the committed source and retained
max-mode SLSA provenance and SPDX SBOM attestations. The tag was absent before
build and immediately before publication; no existing tag was overwritten.

- CP OCI index: `sha256:84a1b5e02ca28af811e72bd7c97cbc6c5337665aac3a708f07087edb9fac10bc`.
- Linux/amd64 runtime: `sha256:958f30efe7b66c0f3a4e64d2e5f8b124b19638e86cd754528f4a2661205cc9c4`.
- SLSA/SPDX attestation manifest: `sha256:c3d51ebe79a7271637be55bed3a1b70dd34e49ec1c0cde2e1abf71b4dada354e`.
- Publication receipt SHA-256: `733370b6b43176722452229b43229bd0ef661a4520b554f6b7909fed0b3a900d`.
- Provenance JSON SHA-256: `289351b8be21421a223dba25c08b121af5be7022264c185b95a0815419954380`.
- OCI archive SHA-256: `2f6a114aa203f1f42b4b8dac0d6a370d31312117b450cd1c1b63203b9e8b1803`.
- Remote image-config receipt SHA-256: `3776e5276e1438c6c9cc213d5249d6e94f64506691794dc2e220ce230d2e8339`.

Protected build/publication directory:
`/home/tux/secure-handoff/cosmos-t2i-defaults-build-20260917-XRqpUzQ1`.
The CP lock/Dockerfile/context-policy hashes remain exactly those in the build
inputs table. The authorized `sandbox2` profile was always selected explicitly;
no default-profile setting was changed. The isolated registry login was removed
and its absence verified. This lane did not mutate the cluster or claim public
T2I success from the local contract tests; the original failure remains retained.

## CP-only generic T2I schema alignment

The typed-default correction above was deployed as release 144, but a bounded
cross-contract follow-up found the generic Cosmos T2I schema still advertised
the same unsupported optional field. Correction
`ad819a0118b7fcd113ce68d3e597d475c0813582`, tree
`f9837ec1147ef18e9479c5edaeecd4b5cd5a5236`, removes `output_delivery` from that
T2I branch and describes the outer union field as video-only. Existing
mode-specific exclusions now reject it for T2I. T2V retains both legacy inline
and artifact delivery; all other video contracts are unchanged.

The exact boundary is **schema-aware named-native validation before admission**
(`cosmos3_nano_native`) and truthful `get_model_schema` discovery. The legacy
opaque `invoke_model` envelope accepts arbitrary model payloads and is not
claimed to provide this model-specific validation. No new HTTP/generic
validation layer, dispatcher behavior or runtime normalization was introduced;
valid T2I without the field remains compatible through that opaque route.

**179 focused tests passed** in 57.27 seconds, with all five generic/specialized
branches checked against actual YAML adapter DTOs, T2I rejection through the
real named-native MCP transport, valid opaque T2I compatibility, T2V legacy
inline delivery, binary artifacts and existing Magpie behavior. Ruff and
whitespace checks passed. An initial test incorrectly expected the opaque
route to enforce the named schema; its failure exposed the boundary above,
and the test was corrected without broadening production behavior. This is
not a live inference or client qualification claim.

Published Linux/amd64 tag: `cosmos-stockholm-ad819a011-20260917` in the same CP
repository. Exact-source/context, packaging, provenance and remote descriptor
verification passed; all 426 build-context files matched Git. The target was
absent before build and publication. The explicit authorized `sandbox2` profile
and separate private Docker config were used, no default setting was changed,
and the temporary registry login was removed and verified absent.

- CP OCI index: `sha256:849020eabbcf07d07112bcebae4032639e8e995a9eda078410ccf4879a188ee8`.
- Linux/amd64 runtime: `sha256:2b59afc1472c7ca9730d20a413586a3b18772a6f5c521abd9b5be7926f201f5c`.
- SLSA/SPDX attestation manifest: `sha256:cfebe01a6bc8c792d869692002d18fa38a7459f6bd8e5c664598b49ba1454d9d`.
- Publication receipt SHA-256: `ddee02a30035449689baace97c5d4ac53f1e4cd3e0380ed7db668c5908ea27a2`.
- Provenance JSON SHA-256: `f0e216be2693acf969f41e217dd6165087ea95f28736c145b582ae25b480c726`.
- OCI archive SHA-256: `c46afe7d24e05d36508d8fbaa703261e7f6a76554fea71ec4c2c142e44b91cde`.
- Remote image-config receipt SHA-256: `3b4da2fe7e995b73b0dcd8b41d60fd18228edf8f05f6753ce1f2d8d69af342cc`.
- Final focused test log SHA-256: `d65027d16e3ffabf58f0e9e93916021abedfed5303931a3a469187d522509f29`.

Protected directory:
`/home/tux/secure-handoff/cosmos-t2i-generic-build-20260917-nwj6cyCw`.
Lock, Dockerfile and context-policy hashes remain unchanged from the table above.
Admin `6428b350...`, schema 32, Cosmos adapter/template/runtime/snapshot and
customer settings remain unchanged. Root owns release deployment and fresh
public acceptance; this build lane made no cluster mutations.

### Release-145 bounded operational readback

At **2026-09-17 15:29:56 UTC**, all three CP replicas and both model-controller
replicas were Ready on exact index `849020ea...`; the unchanged admin remained
Ready 2/2. Each CP mounted the same `608b68ad...` envelope and `d556dcfd...`
bundle data hashes, preserving 20 model qualifications, 23 template revisions
and five voice qualifications. This is a local contract check, not a new public
speech discovery or inference claim.

All three `/metrics` responses were HTTP 200 with 168 customer-operation and
165 semantic-exchange series per pod. Actual Prometheus targets matched those
three current pods, were up without scrape errors, and returned both families.
All 13 rules had healthy evaluation and no evaluation errors. Both new
failure-rate rules remained inactive; the two existing lifecycle alerts and
three existing certificate alerts were **firing**, not resolved or silenced.

GPU observers were **15/15 Ready and Available, 12/15 updated** at this instant.
Readiness does not prove homogeneous observer versions, uninterrupted fleet
coverage or exact per-request attribution when model replicas are ambiguous.
The previous 14/16 observation remains retained rather than rewritten.

Protected receipt:
`/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/final-deployment-verification-r145.json`,
SHA-256 `8062cf90707b03e3d98ae19f88044002eea986f2cde0d995313945243de31c95`.
The release owner's public-surface receipt was not present at capture, so that
gate remains explicitly separate and was not duplicated. This one read-only
pass performed no inference, admission, key creation, cleanup, alert change or
live mutation. It verifies running-image/contract/telemetry availability, not
Helm-history completion or customer/model qualification.
