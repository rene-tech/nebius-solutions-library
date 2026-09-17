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

The scheduled source workflow is configured for 03:17 UTC daily. It installs a
specific Trivy archive only after checking its SHA-256, records the locally
built OCI digest, emits complete JSON results and SPDX JSON SBOMs, and retains
the receipt bundle for 90 days. It scans the control-plane and admin images,
the catalog runtime filesystem/lock, and every accepted entry in the
third-party inventory. A new critical result has a 24-hour remediation SLA; a
new high result has a seven-day SLA, measured from the scanner result
timestamp. A failed or missing scan, incomplete inventory, tag-only subject,
or missing receipt blocks promotion rather than extending the SLA or converting
the result into a pass.

## SAI-24 source changes

- The control-plane runtime installs `libuuid=2.41.6-r0`, the fixed package
  version identified by the review, alongside its existing exact Alpine
  security package pins.
- The admin-console runtime refreshes only `libcrypto3`, `libssl3`, `libexpat`,
  and `libuuid`. The exact installed versions must be recorded from the built
  candidate and the digest must pass the release gate before integration.
- The catalog runtime moves from vulnerable `cryptography==41.0.7` to the
  repository's already locked `cryptography==50.0.1` artifact set.
- Every Nebius Terraform root uses the exact provider constraint `= 0.5.232`
  instead of the unbounded `>= 0.5.232` constraint.
- The existing observability source installer remains usable. Promotion is a
  separate fail-closed gate: `security/third-party-images.lock.json` records
  each known source reference and rejects release while the rendered inventory
  is incomplete or any accepted digest is absent.

## Required integration work

The coordinator boundary for this source candidate forbids registry access,
image pulls, builds, and scanners. Consequently, the admin package revisions;
the website Node base digest; and the CNPG, Envoy Gateway,
kube-prometheus-stack, Loki, Tempo, and OTel image digests are intentionally
not guessed. The DCGM entry reuses an exact digest already accepted in the
current source tree; no rejected historical digest packet is reused. An
authorized integration worker must render every chart, resolve the complete
inventory, add digest-bound values, update the lock state to `digest-pinned`,
run the scanner contract against each exact digest, and retain its receipts.
Until then this candidate is **SOURCE NO-GO**. Observability remains installable
from the existing tagged source configuration, but that path cannot satisfy or
bypass the promotion gate.

Rollback is a normal Git revert of the integration commit plus restoration of
the previously recorded first-party image digests or Helm revisions. The
integration packet must capture those previous identities before any rollout.
