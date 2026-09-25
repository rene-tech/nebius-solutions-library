# Molecular-dynamics starter examples — 25 September 2026

Status: **deployed and qualified for the starter-data scope below**. All 17 new
recipes passed twice. The immutable image is the default `examples/v3/` in Helm
revision 230. The bucket canary, two sequential seeded-client cohorts, all-bucket
verification, automatic new-workspace seeding and temporary-access cleanup passed.

## What is being qualified

Five workflow examples use one canonical ACE–ALA–NME molecule in ff14SB/TIP3P:
quickstart, full 1 ns, native restart, two independently seeded replicas, and
one GROMACS umbrella window. The first four support GROMACS, NAMD, Amber and
LAMMPS, giving 17 new recipes. Canonical inputs contain 6598 atoms, including
2192 waters. The same topology/conversion provenance is retained; this release
does not silently substitute an engine, force field or shortened full run.

The approximately 54 MB candidate preserves the previous 120 cases and 143
recipe/input pairs and adds five MD cases. Large reference trajectories are
not copied into every bucket. A user's new run writes operation/job-scoped
outputs to their own bucket and also exposes checksum-addressed downloadable
artifacts through the platform API.

## Evidence requirements

[Native input qualification](native-validation.json) contains all 34 successful
executions, exact hashes, engine identities and GPU job-admission records.
[Bucket output qualification](bucket-output-validation.json) checks the actual
customer copies of all four quickstart and full-protocol results. The two native
cohorts overlapped, and a backend recovery rollout occurred without replacing
their native runtime images. They are not presented as the separate sequential,
unchanged-release seeded-client gate.

Measured full-protocol accepted-to-terminal time on H100:

| Engine | First run | Independent repeat |
| --- | ---: | ---: |
| GROMACS | 227.0 s | 235.9 s |
| NAMD | 306.3 s | 322.1 s |
| Amber | 202.6 s | 205.6 s |
| LAMMPS | 3765.4 s | 3721.6 s |

These include minimization, equilibration, production, startup and publication
after admission. They exclude pre-admission client backpressure and download
time; some requests waited considerably longer while the same concurrency-three
test key held two long LAMMPS slots. These are not isolated kernel benchmarks,
cold-start measurements or service-level promises. Cold-start telemetry remains
unknown where the platform did not record it, not zero.

- Execute all 17 inputs twice with ordinary, four-App-restricted customer
  credentials through the public MCP endpoint. Keep operation IDs, immutable
  request/recipe hashes, native software/runner identities and failed attempts.
- Verify downloaded SHA-256/length, completed native stages, every saved atom
  and frame, finite cells/coordinates, requested duration and cadence, peptide
  connectivity and density. Generate phi/psi from the native trajectories.
- For the umbrella tutorial, check the 0.1 ps native pull observations against
  angles independently calculated from the 1 ps coordinate frames. A successful
  window does not establish a global PMF or validate an entire WHAM study.
- Verify actual customer-bucket output copies with bucket-scoped credentials,
  not only the platform's artifact store.
- After publishing the immutable data image, download the seeded client and
  inputs from a canary bucket and exercise them. Verify a newly provisioned
  workspace receives the default pack without operator seeding.
- Verify create-only backfill, old-version retention and disabled/excluded
  storage. Preserve current gateway, admin, model and recovery configuration
  during the seed-only rollout.

Native restart is not CUDA/GPU snapshot restore. The short examples test usable
scientific workflows, not equilibrium or converged basin populations. Successful
native execution is not independent proof of cross-engine force-field equality.
Historical v2 proofs qualify unchanged inputs only, not a new all-model benchmark.
This work does not qualify a LibreChat browser release.

## Live delivery and customer-client gate

[Deployment and bucket evidence](deployment-backfill-receipt.json) records the
seed-only canary revision 229 and default revision 230. The exact installed
chart was recaptured for each apply; strict comparison allowed only the seed
init container and seed environment changes. Recovery gateway/controller image
`3840f45...`, native companion image `719ec33...`, admin and model definitions
were preserved. No cloud, customer storage or concurrency quota was increased.

The existing canary and a newly provisioned default workspace each downloaded
all **498 objects / 53,900,577 bytes** with their ordinary, bucket-scoped
credentials and passed every checksum. The new workspace completed seeding on
its first attempt at 07:00:41 UTC, without manual object copying. All 425 old v2
objects in the existing workspace remained byte-identical.

Full-byte readback passed for **16 distinct eligible buckets**, including the
temporary test workspaces: 7,968 object verifications. After the two MD test
identities were disabled, the 07:15 inventory retained 14 ready buckets, all in
that verified set and all reporting v3 complete. The original disabled-storage
policy remained disabled. These are timestamped inventory counts, not a promise
that the number of active tenants never changes.

[Seeded-client evidence](seeded-client-validation.json) records two consecutive,
nonoverlapping cohorts on unchanged revision 230:

| Cohort | Customer workspace | Client start → final download complete (UTC) | Result |
| --- | --- | --- | --- |
| 1 | Existing workspace, bucket-downloaded v3 | 06:58:32 → 07:05:32 | All four engines passed |
| 2 | New automatically seeded workspace, separate key/bucket | 07:06:26 → 07:13:22 | All four engines passed |

Each cohort used the published client and inputs with concurrency three, and
ran minimization plus 20 ps NVT, 20 ps NPT and 20 ps production for each engine.
All eight native results passed trajectory analysis and actual customer-bucket
export verification. The complete 1 ns/restart/replica/umbrella recipes were
already executed twice in the separate 34-run input qualification above; the
delivery gate does not relabel these short client checks as long simulations.

Completed local replay was tested for all four engines with network-client
creation forbidden: it verified retained artifact checksums and returned the
original operation without resubmission or receipt changes. This is a local
client replay check, not a new server-side idempotency/load benchmark.

Both temporary keys now reject access, their owners and storage are disabled,
and their buckets/results are retained as evidence. Direct Kubernetes checks
found none of the eight final-cohort native Jobs remaining. Gateway, controller
and admin deployments were fully ready; Helm remained at revision 230. The
parallel recovery worker owns and retires its separate test identities.

## Retained development failures

An initial Amber batch used nine-digit random seeds. Its native validation
reported an overflowed printed field. The corrected independently seeded jobs
use eight digits and retain the same scientific checks. Failed operation
`f8421cdf-369f-433d-867d-09371581b326` remains negative evidence; corrected
operation `0ff4892a-cc90-402a-b542-d1355b6db66d` passed both replica outputs.

That failure exposed a shared failed-native diagnostic upload identity collision
(HTTP 409). It is tracked separately as
`fs2-failed-native-diagnostic-upload-identity-r20260925`. No claim is made that a
starter-data change fixes that frozen runtime path.

One repeat LAMMPS batch client encountered a transport `ExceptionGroup` before
upload or admission. The prepared receipt and original idempotency key were
retained for resumption; no new operation was invented. This interruption must
remain in the evidence rather than being hidden as a clean first attempt.

## Source, deployment and retained data

Source and build instructions:
[`starter-data/MD_EXAMPLES.md`](../../starter-data/MD_EXAMPLES.md).
Task: `fs2-md-bucket-starter-data-r20260925`.
Parallel operational fix: `fs2-admitted-unschedulable-pool-recovery-r20260925`.
Its recovery-policy qualification is distinct from this pack's native-input
qualification; a shared rollout does not make either evidence interchangeable.

Private credentials, exact storage identities, installed chart values, raw
outputs and full intermediate receipts are retained outside Git in
`/home/tux/secure-handoff/fs2-md-starter-20260925/`. Only payload-free release and
validation summaries belong beside this document. No customer quota is raised.
