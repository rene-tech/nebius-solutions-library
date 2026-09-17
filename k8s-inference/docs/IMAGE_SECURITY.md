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
Kubernetes inference CI as well as being scheduled for 03:17 UTC daily. It installs a
specific Trivy archive only after checking its SHA-256, records the locally
built OCI digest, emits complete JSON results and SPDX JSON SBOMs, and retains
the receipt bundle for 90 days. It scans the control-plane and admin images,
the catalog runtime filesystem/lock, and every subject in the render-derived
release closure, including chart hooks/init containers and catalog model
runtimes. A new critical result has a 24-hour remediation SLA; a
new high result has a seven-day SLA, measured from the scanner result
timestamp. A failed or missing scan, incomplete inventory, tag-only subject,
or missing signed receipt blocks promotion rather than extending the SLA or
converting the result into a pass. First-party receipts bind the BuildKit
provenance, retained OCI archive, manifest digest, commit, and tree. All scan
receipts and the exact render packet require a detached signature under the
independently reviewed Platform Security trust root.

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
  installer consumes `security/third-party-images.lock.json` through the same
  fail-closed post-renderer. It rewrites reviewed tags to digests and rejects
  every unknown tag or unlisted digest; observability is not removed.
- `security/release_image_closure.py` discovers the Terraform Helm resources
  from source, checks all direct installers, requires hash-bound chart, values,
  and render artifacts for every release surface, and includes digest-pinned
  catalog runtime images. A checked-in boolean cannot assert completeness.

## Required integration work

The coordinator boundary for this source candidate forbids registry access,
image pulls, builds, and scanners. Consequently, the admin package revisions;
the website Node base digest; and the CNPG, Envoy Gateway,
kube-prometheus-stack, Loki, Tempo, and OTel image digests are intentionally
not guessed. The DCGM entry retains the exact digest already present in the
current source tree, but it is not accepted without the new resolution
provenance and signed render/scan evidence; no rejected historical digest packet is reused. An
authorized integration worker must render every exact chart+values surface,
record registry resolution provenance, populate the package and image locks,
obtain the reviewed security trust key, sign the render packet, scan every
derived closure subject, and sign and retain its reports, SBOMs, OCI archives,
and receipts for 90 days. Until then this candidate is **SOURCE NO-GO**. The
installers remain present and become usable once those mandatory inputs are
accepted; no tag/default path can bypass the post-render gate.

Rollback is a normal Git revert of the integration commit plus restoration of
the previously recorded first-party image digests or Helm revisions. The
integration packet must capture those previous identities before any rollout.
