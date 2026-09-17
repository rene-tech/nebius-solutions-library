# Image vulnerability and dependency pinning policy

This policy is a source candidate for SAI-24. It does not constitute a scan,
image publication, integration approval, remediation claim, or deployment
approval.

## Release gate

Every first-party and third-party runtime image must be identified by an OCI
`sha256` digest and scanned for vulnerabilities and secrets before promotion.
A candidate is rejected when it contains a fixable `CRITICAL` or `HIGH`
finding, or any secret finding. The scan receipt must bind the source commit,
source tree, image repository and digest, scanner/database identity, command,
timestamp, and complete result counts. A tag-only result is not evidence for a
deployed image.

The image-security workflow is a required reusable dependency of normal
Kubernetes inference CI as well as being scheduled for 03:17 UTC daily. Pull
requests run only an untrusted, unsigned source scan and never receive signing
authority. Non-PR release runs additionally require the protected
`sai24-release-attestation` environment, GitHub's short-lived OIDC identity,
and an authorized signing broker; no long-lived signing key is inherited or
written to the runner. The workflow installs a specific Trivy archive only
after checking its SHA-256, records the locally built OCI digest, emits
complete JSON results and SPDX JSON SBOMs, and retains the receipt bundle for
90 days. It scans the control-plane and admin source candidates and catalog
filesystem in the untrusted lane. The protected lane separately scans every
production digest in the render-derived release closure, including first-party
control/admin images, chart hooks/init containers, and mapped catalog model
runtimes. A new critical result has a 24-hour remediation SLA; a
new high result has a seven-day SLA, measured from the scanner result
timestamp. A failed or missing scan, incomplete inventory, tag-only subject,
or missing signed receipt blocks promotion rather than extending the SLA or
converting the result into a pass. First-party receipts bind the BuildKit
provenance, retained OCI archive, manifest digest, commit, and tree. All scan
receipts and the exact render packet require a detached signature under the
independently reviewed Platform Security trust root. Local source-build scan
subjects are explicitly not accepted as substitutes for rendered production
digests.

## SAI-24 source changes

- The control-plane runtime installs `libuuid=2.41.6-r0`, the fixed package
  version identified by the review, alongside its existing exact Alpine
  security package pins.
- The admin-console runtime refreshes only `libcrypto3`, `libssl3`, `libexpat`,
  and `libuuid`, and accepts them only as exact `name=version` constraints from
  `security/alpine-runtime-packages.lock.json`.
- The catalog runtime moves from vulnerable `cryptography==41.0.7` to the
  repository's already locked `cryptography==50.0.1` artifact set.
- Every Nebius Terraform root uses the exact provider constraint `= 0.5.232`
  instead of the unbounded `>= 0.5.232` constraint.
- Every Terraform Helm release and direct add-on, bootstrap, and observability
  installer consumes both `security/third-party-images.lock.json` and
  `security/first-party-images.lock.json` through the same fail-closed
  post-renderer and validates their signed authority against
  `security/image-attestation-trust.json`. It rewrites reviewed third-party
  tags to digests, accepts exact attested first-party digests, and rejects every
  unknown tag or unlisted digest; observability is not removed.
- `security/release_image_closure.py` discovers the Terraform Helm resources
  from source, checks all direct installers, and requires an out-of-tree signed
  render packet. Every render provenance record binds an authorized builder,
  the exact chart archive and its `Chart.yaml` name/version, ordered values,
  the hashed Terraform resource or direct-install declaration, post-renderer
  and lock identities, and rendered YAML. The packet source commit/tree must
  equal the checkout, but the packet is never checked into that tree, avoiding
  a self-referential Git identity. A checked-in boolean cannot assert
  completeness.
- `security/catalog-images.lock.json` maps `registry.example.invalid` source
  identities to real production repositories while preserving the manifest
  digest. Every mapping and every third-party tag resolution requires a signed
  semantic receipt whose manifest bytes hash to the OCI digest, whose registry
  matches the repository, and whose resolver identity is explicitly allowed by
  the trust policy.

## Required integration work

The coordinator boundary for this source candidate forbids registry access,
image pulls, builds, and scanners. Consequently, the admin package revisions;
the website Node base digest; and the CNPG, Envoy Gateway,
kube-prometheus-stack, Loki, Tempo, and OTel image digests are intentionally
not guessed. The DCGM entry retains the exact digest already present in the
current source tree, but it is not accepted without the new resolution
provenance and signed render/scan evidence; no rejected historical digest
packet is reused. An authorized integration worker must render every exact
chart+values surface, produce the structured render-provenance records and
signed packet outside the Git tree, record signed registry-resolution and
catalog-mapping provenance, populate the package and image locks, and configure
the independently reviewed trust key, allowed builders/resolvers, protected
workflow refs, evidence artifact identity, and OIDC broker. The protected
workflow then scans every derived production closure subject and signs and
retains its reports, SBOMs, closure, and receipts for 90 days. Until those
currently null/blocked values are independently resolved, this candidate
remains **SOURCE NO-GO**. The installers remain present and become usable once
those mandatory inputs are accepted; no tag/default path can bypass the
post-render gate.

Rollback is a normal Git revert of the integration commit plus restoration of
the previously recorded first-party image digests or Helm revisions. The
integration packet must capture those previous identities before any rollout.
