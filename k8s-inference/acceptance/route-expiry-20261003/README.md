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

## Live release

- Runtime source: `e275c94918d9400e2f6809f4cf06cfe21e4e8e27`, pushed to the existing
  `agent/fs2-customer-workbenches-20261002` integration branch.
- New API image/index: `sha256:6a2876b380f43717ea37cb21dd4504af5884c9a305c486562a42ef103420f946`.
  Linux/amd64 manifest: `sha256:7b9760421feb09a66edd3c10d04e71c887cda09d299f8464b16e7bb8d86c3759`.
  Existing repository: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane`.
- Built with the existing committed-source `scripts/build_image.py`, unchanged
  dependency lock, SBOM and provenance. Digest verified after registry upload.
- API-only compare-and-swap patch: `api-image-patch.json`. All three API replicas
  Ready/Available, deployment generation 354. Controller/admin/TLS/HTTPRoutes and
  protected Lynx LibreChat remain unchanged. Initial 40/50-second rollout watches
  timed out while containers started; the rollout subsequently completed normally.
- On the actual deployed image, isolated Registry loads using the real mounted
  configuration passed at current time and +3,650 days. The speech registration
  stayed enabled with `valid_until: null`. Real `/readyz` returned 200 with route,
  admission, scientific-worker, federation and dynamic-publication health ready.
- Expanded offline suite: 137 passed, plus a separate passing read-time per-model
  expiry regression; Ruff passed. The original 107-test suite is a subset, not
  additional independent coverage.
- Public routing checks passed 9/9 before and after rollout. Lynx's unchanged key
  can list all five MD Apps and read its historical operation. Admin customer,
  workbench inventory and scientific-run listing all return 200 (the latter
  returned 503 before repair).

## Existing API keys

Audited 78 key records, of which 15 were active. Thirteen already had no expiry,
including Lynx, Kopra, robotics and the main Rene key. Cleared only `expires_at`
on the two remaining active keys via the admin API:

- `clinical-speech-round2-20260925`: deadline was 2026-10-03 23:59:59 UTC.
- `scientific-video-factory-20260918`: deadline was 2027-03-17 19:03:09 UTC.

Readback verified unchanged key IDs/fingerprints, grants, scopes, concurrency and
budgets. All 15 active keys now have no expiry. No revoked or expired historical
qualification key was reactivated. Secrets were never printed or committed.

## Public REST execution

`verify_gromacs_rest.py` reuses the shipped client uploader and checksum-verified
artifact downloader, but submits, polls and fetches results through public REST.
It uses the existing `system/qa` key, not a new tenant and not Lynx's key. Each
cohort repeats the same idempotency key and verifies the original operation is
returned rather than submitting duplicate GPU work.

The unchanged seeded alanine ff14SB/TIP3P input runs minimization, 20 ps NVT,
20 ps NPT, 20 ps production, energy extraction and trajectory checking. These are
complete short workflow acceptance runs, not new converged scientific benchmarks
or a requalification of every model. Both cohorts passed all 13 native steps,
semantic validation, idempotent replay, and checksummed artifact retrieval.

| Cohort | Operation ID | Admission to terminal | Including upload/download | Verified artifacts |
| --- | --- | --- | --- | --- |
| 1 | `f89068a8-9e7e-4c30-bfe9-01f564be6286` | 81.742 s | 96.503 s | 56 |
| 2 | `4976b074-059a-4562-8ff5-277a425e970a` | 80.708 s | 97.335 s | 56 |

The unchanged scheduler selected existing `h100-ondemand-1x` capacity, one GPU
per sequential job. Both attempts report `resource_released: true`. No node,
tenant, key, bucket or endpoint was provisioned for these tests. Evidence and QA
outputs are retained; no customer content was deleted. Source input hash:
`8d2d7f61ddb7d329387fc64b2b36b27511f8575cfcaffdf2bc0763ed579f008e`.
Machine-readable evidence: `results.json`.

Private build/operation/artifact evidence:
`/home/tux/secure-handoff/fs2-route-expiry-20261003/`.
