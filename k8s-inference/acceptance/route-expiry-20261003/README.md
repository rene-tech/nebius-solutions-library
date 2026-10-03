# Route expiry availability repair — 2026-10-03

Task: `fs2-route-expiry-availability-r20261003`, NIM Fast Start Platform.

## Incident

The internal registration for `nemotron-speech-en-medical-0-6b` was signed on
2026-10-02 at 06:00:08 UTC with an evidence expiry of 2026-10-03 05:00:08 UTC.
The 15-second registry revalidator treated that 23-hour evidence window as a
service lifetime. Its all-or-nothing failure path withdrew every serving route
and made all three API replicas unready. This was not an expired Lynx API key.
Restarting unchanged code cannot fix it.

Production reproduction: loading the exact projected files at 04:59 UTC
succeeds; at current time it raises `signed attestation is not fresh at
validation time`; omitting just that optional registration in an isolated
interpreter succeeds. No live configuration was changed for reproduction.

## Repair

- Durable native registrations verify signatures, trusted signing identities,
  subject hashes, grants and model identity without inheriting the evidence
  envelope's expiry as a service deadline. No artificial future date or periodic
  manual renewal. They disappear when removed or trust is revoked.
- Optional native registration errors are isolated per model, including startup
  and file-level errors. Valid unrelated routes remain available. Reload starts
  from base configuration; it never retains a removed or tampered route.
- A runtime validity check withdraws only expired model routes, not the whole
  registry. Globally invalid base configuration still fails validation; dynamic
  controller leases and explicit historical evidence validation are unchanged.
- Customer key issuance already defaults to no expiry. A regression verifies a
  default key ten years later, and still checks explicit revocation.
- No customer-key rotation, new authentication requirement, data migration,
  quota change, TLS change, or Lynx LibreChat modification.

## Verification and deployment

Source worktree: `fs2-customer-workbenches-20261002`, same production lineage.
Pre-repair API image: `sha256:f8e30440c7296e7d0d36ffd8b6763b06109b05722371a77b757a3f8eea82f053`.
Model-controller and admin images are deliberately not changed.

Focused regression suite initially passed 107 tests (route reload, signatures,
grants, revocation, scientific availability, lean routes and access accounting).
Website/API public routing checks passed 9/9 before rollout, with real TLS and
hostnames. This does not mean API readiness passed before repair: all three
replicas were unready and the artifact fallback service still answered reads.

Deployment identity, final tests, customer-key metadata audit and two complete
GROMACS operation/artifact receipts will be added after live verification.
