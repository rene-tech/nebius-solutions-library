# WhiteLab access and single-cell handover — 7 October 2026

Status: **in progress**, not a general production/biological qualification.
Requested delivery: REST + MCP + Nebius-branded LibreChat, shared 1,000 GB
workspace, eight concurrent operations and no scheduled API-key expiry.

## Initial reliability release (R1)

Source `a29ca0d85986a1164692bb197dd5c079dcc7ef2e` in the maintained
`rene-tech/nebius-solutions-library` fork. Backend amd64 manifest:

`sha256:a8fb464f2b383b099b892909f39317a7a5275c90cd88e835248b083c97ed76da`

Image repository:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane`.
All API replicas (3/3) and controller replicas (2/2) rolled out; maintenance and
scientific collector tools reference the same release. scVI training runtime
remains `sha256:063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246`.
No customer jobs, routes, keys, node pools or scheduling contracts were reset.

The old failed operations `5f983d94-126b-426a-993b-35c0b623eff3` and
`5c197c82-9851-4704-b74f-39b249680632` contained receipts missing `job_id` and
`checkpoint_generation_created` (absent, not explicit null). They now publish
an honest failed result, `SCIENTIFIC_DIAGNOSTICS_INVALID`, without an output
manifest. Original artifacts remain unchanged. They no longer retry forever or
poison shared readiness. Storage/network/database and global configuration
errors still propagate; successful scientific output validation is unchanged.

The current scVI failure collector was also incomplete. It now writes a
properly bound `native-failed-diagnostics/v1` artifact alongside its failed
result. Live deliberately invalid-input operation
`73f74eaf-b510-4f18-89de-9d9f1e7c56c0` failed as expected and published both
verified artifacts. Its diagnostics contain `job_id: main` and
`checkpoint_generation_created: false`; readiness remained HTTP 200 with zero
consecutive scientific reconciliation failures.

Tests: 206 platform/client regressions passed; eight database-fixture tests
were skipped because no test database was configured. Five additional release
helper tests passed. Existing immutable ConfigMaps are reused only when their
data bytes match; an older generator cannot accidentally unset immutability.

Rollback backend manifest:
`sha256:f64cd4b39a6eaf41e1762837e0a878ee7a78307f973ef6a822a29b118b5510df`.
Exact template-test/replace rollback patches and prior configuration are in the
private evidence directory. Rolling back also reintroduces the diagnostic bug;
do not do so casually. The new execution map is `fs2-scvi-execution-46274a3abe21`;
the scheduling map remains `fs2-scientific-scheduling-75bdc12794d2`.

## Real concurrency evidence

`run_cohort.py` reuses finalized public-data artifacts and the shipped customer
REST/MCP client, alternating both interfaces. Each submitted operation retains
its immutable request, idempotency replay, status/result, downloaded artifact
hashes and full-cell output validation. Only `system/qa` credentials are used.

- Cohort A: eight simultaneous 584,944-cell HLCA scANVI jobs, seeds 42–49,
  upstream scVI epoch heuristic + 20 scANVI epochs, all cells preserved.
  All eight passed artifact and row/probability validation. Accepted-to-completed
  times: 896.91, 1009.36, 842.41, 992.33, 839.26, 829.42, 1057.08, 961.88 seconds
  (13.82–17.62 minutes). This is complete job latency, not only GPU training.
- Two real MD jobs overlapped. The REST run succeeded; the MCP run failed
  natively after this harness extended a 10k-step free-energy timing fixture to
  100k steps. GROMACS reported an excluded perturbed pair outside the neighbor
  cutoff. This failed run and all diagnostics are retained; **A is not claimed
  as a clean mixed cohort**. No customer input or force-field cutoff was changed.
- Follow-up cohorts B/C restore the fixture's previously tested 10k-step
  protocol (three independent simulations and native energy extraction), with
  four million-cell atlas and four routine single-cell jobs each. B passed all
  ten; C's interrupted polling clients recovered the original operations.
  See retained findings below rather than only the final successful artifacts.
- An attempted ninth operation with the eight-slot QA key returned HTTP 429
  `concurrency_exceeded`, without a new job. This endpoint currently supplies
  no Retry-After header; clients should retain running IDs and wait for a slot.

Public `/readyz` is sampled throughout. Native scientific failures remain visible
to their own callers, without making unrelated scientific workers unavailable.
These runs validate platform execution/data integrity, not biological accuracy
or convergence. No eight-atlas, multi-GPU training or GPU-process snapshot claim.
Training recovery remains the qualified full-Lightning-checkpoint mechanism.

## Customer resources

Tenant `whitelab`; initial contact/principal `artemis` (Artémis Llamosi, confirmed
in the existing customer Slack channel). Shared bucket
`fs2-whitelab-951ca2389f78c7ca`, configured quota **1,000,000,000,000 bytes**.
Key `whitelab-poc`: eight concurrent operations, no expiration. The credential
itself is never stored in this repository or included in public evidence.

Granted Apps: Boltz2, DiffDock, ESMFold2, ESMFold2-Fast, OpenFold2, OpenFold3,
OpenFold3 OpenBind, GROMACS, GROMACS MPI, NAMD, LAMMPS, BoltzGen, RFdiffusion,
ProteinMPNN, MSA Search PDB70 and scVI/scANVI. Access/catalog checks confirmed
exactly this set. Existing Apps are not all scientifically requalified by this
single-cell release.

## Agent-discovered fixes and rollout test

The actual agent used a shared App that exposes both native and batch protocols.
`get_model_schema` incorrectly returned only the native contract, and selecting
the batch tool by name incorrectly failed before batch discovery. Backend source
`b768003f07dc5fd5512f5bb757cbdfc873c33fdb` fixes both paths and preserves specific
selectors, caller grants and artifact schemas. It also completes the missing
GROMACS continuation argument descriptions. 28 MCP contract regressions passed;
ten read-only public MCP checks passed against the real scVI App.

Current backend amd64 manifest:
`sha256:aae7e6f1f7dafe528051d69da07697d62531bc15ea930de4194aaf14c66fcd61`.
The API/controller/maintenance image changed; scientific tools remain the prior
`a8fb464f...` release, with training image and execution/scheduling maps unchanged.
The exact R2 rollback patches are retained privately. Rollback target is R1
`a8fb464f...`, not the older diagnostic-bug image.

B completed all ten operations, including the four actual million-cell atlas
jobs (23.17–23.70 minutes accepted to completion), four routine jobs and two MD
jobs. Every artifact and full-cell output check passed. C overlapped the R2
rolling release: two MCP status clients failed on transient upstream HTTP 503
while their GPU jobs continued. One readiness sample reported database timeout.
**C is not a clean cohort.** Original failures remain retained and no replacement
GPU job is submitted to hide them.

Canonical client source `b033baeb4361305fdbf756a397382de48c9c3a87` now retries
safe reads and explicitly idempotent requests unchanged on connection errors
and HTTP 502/503/504. Retries are bounded (six attempts with 1/2/4/8/10-second
delays); persistent failure and non-retryable responses remain errors. Twenty-one
client/transport tests passed. Recovery of the same two C operation IDs includes
client-local injected 503/disconnect faults, then full artifact validation.
Both recoveries passed: all million-cell rows, all 40 files, unchanged retry
bodies and zero new model submissions. Original client failures remain retained.

D overlapped the actual agent test using the same two-slot internal key as the
MD clients. One MD submission correctly received explicit non-admission; the
previous MCP client stopped rather than waiting. Canonical source `b2aa0c2b2`
adds bounded waiting for this narrow rejection, preserving the request/key and
never treating scientific errors or durable admission as retryable submission.
29 client tests passed. The rejected MD request is recovered separately with a
client-local non-admission injection and idempotency replay. **D is not a clean
cohort**; E/F use the fixed client and remain pending.

Actual LibreChat MCP operation `3b3e52b6-5c3c-4b1a-bf3c-c27a99c1d7f7` trained
all 584,944 cells with the requested 20+20 epochs and succeeded at 08:02:03 UTC.
The chat's initial submission-only turn made an incorrect automatic-local-download
promise. Skill v2026.10.07.2 explicitly documents that exit 75 ends the local
helper and recovery is needed for local collection. The final agent retrieval
test retrieved all 40 artifacts and passed row/probability validation, but exposed
an S3 append-mode bug. The agent worked around it by copying to scratch. This is
not accepted as the desired client experience: the fixed shared helper keeps
separate per-attempt logs directly in the mounted study, preserving older logs.
An independent scVI-only agent operation `9aa9398c-59fe-4884-8fc9-f597dbf651f1`
also exposed the AnnData 0.12.3 / SciPy 1.17.1 backed-sparse incompatibility.
SciPy 1.17 moved the internal indexing method used by that AnnData version;
see [upstream release notes](https://docs.scipy.org/doc/scipy/release/1.17.0-notes.html).
The image pins SciPy 1.16.3 and runs real dense/CSR/CSC H5AD backed-read tests.
R5 fixed-image agent acceptance remains pending. Existing conversations and
receipts survived replacement on the dedicated persistent filesystem.

BindCraft is excluded because the customer explicitly deferred it without a
Rosetta/PyRosetta licence. Standard Evo2 remains their pending choice; hosting
their fine-tune is explicitly outside this PoC. No academic exception invented.
See the customer decisions in Slack thread `1791283815.432749`,
channel `C0BGX8K5QPL`.

Starter pack v3: all 498 objects / 53,900,577 bytes checked against manifest
`c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709` by full S3
readback. Customer S3 credentials cannot list the system QA bucket. No customer
model calls were used for internal testing. LibreChat login awaits the verified
initial email address; no public registration or guessed email was configured.

## Workbench candidate

Source fork `rene-tech/serverless-ai-cookbook`, commits `d9782a9`, `9953c78`.
Immutable image index:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:727b9a002f4839811e9dd31b8ec33cda5e17464513f42405b5e0ca1baeacbb59`
(amd64 manifest `sha256:b7a1c5fa360a6ba0e2dc65949e4bfc8c87560b0885369ce2b0bb2bab53110a04`).

Preserves the October 7 Nebius branding/persistent-state image; adds the pinned
canonical single-cell file clients, recoverable workbench wrapper and corrected
`single-cell-analysis` skill v2026.10.07.1. The old 64 MiB demo path remains
separate. All 32 wrapper/receipt tests passed, skill checksums verified and image
dependencies checked using the installed scientific Python environment.

One temporary QA candidate, `aiendpoint-e00w4g4epattkvnnq6`, reuses existing
system QA API/S3/provider bindings with a distinct study namespace and dedicated
state filesystem `computefilesystem-e00y8j7zypsqv823st`. It does not replace any
customer or the existing QA client. Retain evidence/chats and stop this temporary
candidate at closeout; shared QA storage must not be deleted.

## Evidence and remaining work

Private raw evidence/credentials:
`/home/tux/secure-handoff/fs2-whitelab-final-20261007/` — do not distribute whole
directory. It contains only explicitly separated customer provisioning receipts
and QA execution cohorts; hand over selected customer credentials privately.

Remaining: finish B/C result/availability/accounting verification; qualify the
actual agent flow on the candidate; provision and verify the customer's own
LibreChat binding once its login email is supplied; prepare the private handover.
The [single-cell user guide](../../models/visual-science/scvi-scanvi/README.md)
contains REST/MCP upload, parameters, polling, output and reference-mapping usage.
