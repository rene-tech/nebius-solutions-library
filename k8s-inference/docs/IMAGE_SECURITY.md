# Image vulnerability and dependency pinning policy

This policy is the source remediation for SAI-24. It does not constitute a
scan, image publication, integration approval, or deployment approval.

## Release gate

Every first-party and third-party runtime image must be identified by an OCI
`sha256` digest and scanned for vulnerabilities and secrets before promotion.
A candidate is rejected when it contains a fixable `CRITICAL` or `HIGH`
finding, or any secret finding. The scan receipt must bind the source commit,
source tree, image repository and digest, scanner/database identity, command,
timestamp, and complete result counts. A tag-only result is not evidence for a
deployed image.

The scheduled source workflow is configured for 03:17 UTC daily and uses the same
fixable `CRITICAL`/`HIGH` gate. A new critical result has a 24-hour remediation
SLA; a new high result has a seven-day SLA, measured from the scanner result
timestamp. A failed or missing scan blocks promotion rather than extending the
SLA or converting the result into a pass.

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
- The observability installer refuses to run while its rendered third-party
  images remain tag-only. `observability/versions.lock.yaml` preserves the
  discovered inventory and records the blocked state.

## Required integration work

The coordinator boundary for this source candidate forbids registry access,
image pulls, builds, and scanners. Consequently, the admin package revisions
and the CNPG, Envoy Gateway, kube-prometheus-stack, Loki, Tempo, OTel, and DCGM
image digests are intentionally not guessed. An authorized integration worker
must resolve those references, add digest-bound chart values, update the lock
state to `digest-pinned`, run the scheduled scanner contract against the exact
digests, and retain its receipts. Until then the candidate is **SOURCE only**
and the observability installer remains fail-closed.

Rollback is a normal Git revert of the integration commit plus restoration of
the previously recorded first-party image digests or Helm revisions. The
integration packet must capture those previous identities before any rollout.
