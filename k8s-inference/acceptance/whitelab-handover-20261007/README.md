# WhiteLab access and single-cell handover — 7 October 2026

Status: **technical delivery verified; customer login awaits a verified email**.
This is a scoped research-PoC qualification, not a general production/biological
qualification or a claim that every previously granted App was retested.
Requested delivery: REST + MCP + Nebius-branded LibreChat, shared 1,000 GB
workspace, eight concurrent operations and no scheduled API-key expiry.

Two final unchanged-release mixed cohorts passed **20/20 operations**, including
16 single-cell jobs at 584,944 or 1,000,000 cells and four concurrent MD jobs.
All **187/187 sampled readiness checks** passed. The actual R5 LibreChat agent
completed training, same-operation recovery, reference reuse and authenticated
artifact readback. Tests used internal QA identities, never the customer key.

The customer-managed R5 replacement is running and verified at
<https://port3080-zvfk1qbfp44r0yr.tunnel.applications.eu-north1.nebius.cloud>.
It preserves the existing bucket, non-expiring key and persistent application
state. Only Artémis Llamosi's verified login email is missing; registration stays
closed and no customer-login success is claimed. See [CUSTOMER_GUIDE.md](CUSTOMER_GUIDE.md).

Current exact release: API/controller/maintenance amd64 `aae7e6f1f7dafe528051d69da07697d62531bc15ea930de4194aaf14c66fcd61`,
scientific collector/tools `a8fb464f2b383b099b892909f39317a7a5275c90cd88e835248b083c97ed76da`,
training worker `063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246`,
and LibreChat index `92b18aa638f222d1b5b53c61e3c4cad9e11e5e5abc032e856a6a8d69388086fc`
(all SHA-256). Earlier releases/failures below are retained history.

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
29 client tests passed. The rejected MD request recovered as operation
`69493c8e-9800-4a63-935d-2327aaf8ce24`, including a client-local non-admission
injection, unchanged request and verified idempotency replay. Only one actual
operation was admitted. **D is not a clean cohort**; its original client failure
is retained. E/F used the fixed client and both passed all ten operations.
E passed all 93 sampled readiness checks; F passed all 94. The API image,
training runtime and execution/scheduling contracts were unchanged throughout.

| E workflow | Parallel requests | Cells per request | Accepted to completed |
| --- | ---: | ---: | ---: |
| Routine scANVI | 4 | 584,944 | 14.43–14.90 minutes |
| Atlas scANVI | 4 | 1,000,000 | 24.34–25.24 minutes |
| Existing MD protocol | 2 | Not applicable | 55.80–58.27 seconds |

All eight single-cell runs verified 40 output files each, with full-cell
embedding/label/probability checks. Each interface carried five of the ten
operations. [cohort-e.json](cohort-e.json) retains timings, input/recipe/runtime
identities, resolved scientific settings and validation evidence. Parallel API
admission does not imply every worker starts simultaneously; E observed at most
eight worker Pods in `Running` state at one sample.

| F workflow | Parallel requests | Cells per request | Accepted to completed |
| --- | ---: | ---: | ---: |
| Routine scANVI | 4 | 584,944 | 15.65–16.88 minutes |
| Atlas scANVI | 4 | 1,000,000 | 24.83–25.04 minutes |
| Existing MD protocol | 2 | Not applicable | 54.71–58.81 seconds |

[cohort-f.json](cohort-f.json) retains the second independent cohort. Across
E/F, all 16 single-cell runs verified **640 output files / 18,524,890,537 bytes**,
with full row/embedding/label/probability validation. The datasets are reused
under recorded seeds; this is not a claim of 16 independent biological datasets.
The four MD operations also passed their public-path result/artifact checks.
These are full operation timings, not pure training speed or model cold-start
latency. Client-side result download/validation can finish after remote completion.

The admin run API was queried for all 20 final operations. Every run is attributed
to `system/qa`, not WhiteLab, and has a GPU-allocation ledger. The public projection
is [accounting-evidence.json](accounting-evidence.json); complete run details and
device samples remain in the private evidence directory. Lifecycle-based
allocated/active/idle times are explicitly **estimated**, not measured GPU-busy
time or billing-grade accounting. Missing trace context and incomplete phase
classification remain visible rather than being replaced with fabricated zeros.

E/F explicitly publish repeated single-cell QA results to platform artifact
storage. Earlier cohorts already exercised customer-bucket publication; the
final actual-agent study still uses the S3-mounted workspace. This avoids filling
the shared 100 GB QA bucket with duplicate qualification outputs. It does not
change the customer's 1,000 GB bucket, quotas, or default workflow settings.

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
R5 passed the real direct-mounted recovery of operation
`9aa9398c-59fe-4884-8fc9-f597dbf651f1`: all 584,944 cells, 18 files and
424,932,923 bytes verified in approximately 30 seconds, without moving the study
to scratch or creating another model job. The chat named the validation receipt
under `run/`; the actual helper receipt is at the study root. Use the recorded
filesystem paths, not an inferred path from prose.

The fresh R5 agent scVI operation `d291df6a-d8a5-48ab-9227-73aa82c4d80a`
used every cell and gene, seed 44, 20 epochs and the durable MCP interface. It
succeeded remotely from 08:52:22 to 08:57:11 UTC (289.37 seconds). Collection
through the same chat passed: all 18 files / 425,195,793 bytes, every cell and
embedding checked. The four delivered workspace links were independently read
back through authenticated LibreChat HTTP, including the complete 318.6 MB
H5AD and 79.1 MB embedding table; their SHA-256 values matched. The new chat's
validation-receipt link correctly points to the study root. No new training job
was created during collection. The submission turn accurately distinguished a
running remote job from downloaded results. Existing conversations and receipts survived image
replacement on the dedicated persistent filesystem. Actual Token Factory and
Tavily calls also passed on R5, not just configured-key presence checks.

A separate actual-agent reference-reuse operation
`24467cb6-2001-453e-a7ab-1a2d61252e81` mapped an explicitly selected 4,096-cell
engineering fixture against that completed scVI reference. It preserved all
2,000 genes, used ten query epochs and submitted exactly one mapping operation.
Accepted-to-completed time was 37.72 seconds; worker execution 13.57 seconds.
All 18 outputs / 22,058,276 bytes and every query embedding passed validation.
The agent recovered two **local preparation** errors (AnnData nullable-string
write opt-in and staging HDF5 locally before copying to the object-storage
mount). These are retained limitations, not hidden behind a zero-error claim.
The platform operation succeeded on its first attempt. Query cells were in the
reference training set: this tests transport/reference reuse, not held-out
accuracy. The customer guide now explains the local-HDF5 staging requirement.

## Customer choices and storage validation

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

## Delivered workbench

Source fork `rene-tech/serverless-ai-cookbook`, commit
`83c3ce9250526eb579c54f75adb1ed16d9238c2e` (R5).
Immutable image index:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:92b18aa638f222d1b5b53c61e3c4cad9e11e5e5abc032e856a6a8d69388086fc`
(amd64 manifest `sha256:4e937c4178c02413d8554722e596faa443f224e17fa03ec81e161603063381f7`).

Preserves the October 7 Nebius branding/persistent-state image; adds the pinned
canonical single-cell file clients, recoverable workbench wrapper and corrected
`single-cell-analysis` skill v2026.10.07.4. The canonical client comes from
`b2aa0c2b24e3c3e9d258f9980468d4d1945a38ea` in the backend fork. The old 64 MiB
demo path remains separate. All 34 wrapper/receipt/build-context tests passed; 36 bundled skills
and 78 files passed checksum verification. The image's installed environment
passed dependency and actual dense/CSR/CSC backed-H5AD checks.

The temporary QA R5 candidate used existing system QA API/S3/provider bindings,
a distinct study namespace and dedicated state filesystem
`computefilesystem-e00y8j7zypsqv823st`. Actual login, conversations and receipts
survived its image replacements. R4 was built but never provisioned. No browser
automation was used: actual hosted agent execution, login, workspace and delivered
links were exercised through the authenticated client HTTP interface. This does
not constitute a visual browser-interaction acceptance test.

After E/F, the qualified immutable R5 release was added to the managed workbench
release menu, preserving the existing release. The API rollout completed 3/3;
public readiness and all ten live MCP discovery checks passed again. Only the
release-menu setting changed, not the tested training/scheduling releases. A
targeted rollback and desired Helm fragment are retained; do not overwrite live
configuration with stale whole-release Helm values.

Managed upgrade `8dbe8fdc-1fe9-465f-b95f-8c0eda32b3c1` succeeded on existing
binding `68fb4fd8-cce1-4330-8341-52a766e8a1c9`. Customer endpoint
`aiendpoint-e00zzbdhd3hkmaxtnc` uses the exact R5 index, existing state filesystem
`computefilesystem-e00zpv5gnsct0hhat1`, bucket and credentials. Public health/title,
Nebius logo hash, closed registration, mounted starter manifest, pinned client
hash, SciPy/AnnData versions, persistent database and credential binding all pass.
The private verifier initially assumed Docker's legacy config-ID format; this
host uses the containerd image store and reports the index ID. Matching the
immutable reference and RepoDigests resolved that verifier defect, without any
image change. The original mismatch is retained in private finalization evidence.
The stopped customer R3 predecessor `aiendpoint-e00hw7kkesds7smer8` is retained
for managed rollback. Lynx and Basel were not changed.

The cookbook fork now points future deployments at the qualified R5 release.
Its ordinary directory build context includes the runtime verifier. Build stages
and runtime checks pass, but a local legacy-Docker `--load` attempt hit inherited
layer depth; that limitation is documented in the cookbook's single-cell release
note. The unchanged production image was pulled and tested successfully on
Serverless. No untested rebuild replaced it.

## Evidence and remaining work

Closeout completed at 10:08 UTC: four exact task-only QA endpoints were removed
after exporting their chats and specifications, and the temporary eight-slot QA
key was revoked. Persistent QA state, the shared QA bucket, original QA client/key
and every customer resource were retained. Endpoint containers/local ephemeral
disks are removed; retained specifications/state/evidence allow reconstruction.
The public API and retained customer workbench both returned HTTP 200 after
cleanup. See [closeout.json](closeout.json). No cloud quota, GPU pool or customer
inference limits were changed for these tests.

Private raw evidence/credentials:
`/home/tux/secure-handoff/fs2-whitelab-final-20261007/` — do not distribute whole
directory. It contains only explicitly separated customer provisioning receipts
and QA execution cohorts; hand over selected customer credentials privately.

Remaining customer input: **Artémis's verified login email**. Seed the existing
workbench account and verify its login once supplied. Do not recreate its tenant,
user, API key, bucket or persistent filesystem, and do not rotate their credentials.
The selected API/S3 credentials and instructions are handed to the owner privately;
provider keys and raw QA evidence must not be included in a customer handover.
The [single-cell user guide](../../models/visual-science/scvi-scanvi/README.md)
contains REST/MCP upload, parameters, polling, output and reference-mapping usage.
