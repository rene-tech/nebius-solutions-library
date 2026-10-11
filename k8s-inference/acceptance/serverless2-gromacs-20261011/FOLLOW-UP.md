# GROMACS migration follow-up — 11 October 2026

This is an **internal shared-fleet qualification**, not a customer cutover or
HIPAA/PHI release. The existing Scientific AI cluster, clients, keys, storage,
DNS and running customer jobs were not changed. Keep the application on
`scientific-ai-serverless2-migration`; the Serverless changes remain in its
existing, unmerged PR. The earlier [r5 report](LIVE-PILOT.md) is preserved.

## Exact deployed release

| Component | Source / image | Digest |
| --- | --- | --- |
| Scientific AI API | `57f14fccd`, `pilot-20261011-r10` | `sha256:cb1766c4189e58e8812c24686efb8578c91b086476ea9f1cc07e5c56a9408985` |
| RTX worker, native SM120 | `pilot-20261011-r6` | `sha256:795fe268ece030e3ca7f9dfa740e55a025d30084f247adfcbcf07de84966fcea` |
| L40S worker, native SM89 | `pilot-20261011-r6` | `sha256:be34bbb1a8187eeb2a1dee2a649e4bf7101ce3256ecb7a453d1b8b48d8f7ebf2` |
| Serverless API / billing | `094db8f16`, `0.11.20` | `sha256:37fbd757a59c2e25b48ce83cb7ec0be64f2bd6a1c4fa8e6f49da5230a392de9b` |

The worker runtime is GROMACS 2026.4 / CUDA 12.8.1, single precision, AVX2_256,
thread-MPI/OpenMP. Each attempt requests one GPU, 12 CPU, 24 GiB RAM and 20 GiB
private network scratch. Runtime revision: `gromacs-2026-4-pilot-20261011-r6`.
No CUDA process snapshot is used: these are portable **native MD checkpoints**.

REST: `https://api.195.242.30.226.sslip.io/scientific-ai/v1`.
MCP: `https://api.195.242.30.226.sslip.io/scientific-ai/mcp`.
The API is in `scientific-ai-pilot`; GPU work uses the existing internal
`sciai` tenant, RTX in us-central1 and L40S in eu-north1. Use the private pilot
Terraform state, not the production platform state. No customer key was used.

## Gaps fixed

1. **Storage identity:** the broker now requires a Kubernetes-signed projected
   pod JWT as well as the logical-job capability. It verifies a fixed issuer,
   JWKS, audience, namespace, service account, timestamps and current S2 attempt.
   Worker pods receive no tenant S3 credential. Missing/forged/replaced-worker
   identities are denied. This is not hardware attestation or instantaneous
   Pod-deletion revocation; previously issued object URLs last at most 60 seconds.
2. **Managed issuer compatibility:** public JWKS fetching needs an explicit
   application User-Agent. The managed issuer returned 403 for urllib's default
   client. The correction keeps TLS and all signature/claim validation enabled.
3. **Startup race:** a verified current worker can start before S2's aggregate
   status leaves `QUEUED`. That state now returns retryable 503, not a permanent
   403 and not a signed URL. Missing identity and terminal jobs remain denied.
4. **Continuation delivery:** S2 delivers private input before admitting every
   initial, fallback, resume and export-retry Job. The previous fallback path
   created a Job without its required private Secret. Chain cleanup now deletes
   only the finished operation's copies, not an active successor's copies.
   Fresh portable non-fleet resume also receives its own PVC correctly.
5. **Honest compatibility:** unsupported resume fields return 422 rather than
   silently changing intent. Discovery describes the pilot's one-GPU, one-job,
   explicit-resume limits. It does not advertise batch, MPI or automatic recovery.

The S2 change was deployed with a narrow Terraform plan for API, billing and
additive registry authentication. No GPU pool bounds, shared GPU floors,
reservation, quota or customer resources were changed.

## Tests and live receipts

- Application: **123 passed**, one opt-in native test skipped in the ordinary suite;
  Ruff passes. The separate real-engine CPU recovery gate passed 600,000 steps,
  interrupting at step 240,620; preparation ran once across two attempts.
- Existing GROMACS regression tests: **75 passed** for runtime, inventory cache
  and file expansion. The original native worker's defaults remain unchanged.
- Broader inference/Terraform facade: **324 passed** locally with Terraform
  **1.16.5**. The older GitHub job pins 1.11.4 and previously failed dependency
  imports and facade validations; this local pass does not close that CI gate.
  Portable-source validation
  passes for 4,910 files. The exact-byte history inventory now includes the
  reviewed internal fleet receipts/driver; no runtime code or credentials were
  exempted, and historical measurements were not rewritten.
- S2: **240 passed, 13 optional skipped**, four Terraform tests; `make check`
  and [PR validation](https://github.com/nebius/nebius-solutions-library/actions/runs/38174730095)
  pass. All three application/upstream contract probes pass against `094db8f16`:
  resume idempotency, stable fallback handle and no success without finalization.
- Current REST negatives: anonymous REST/MCP 401, another principal's operation
  and artifact 404, six concurrent replays produce one operation, changed intent
  with the same key 409, four unsupported resume variants 422.

The fixture is fresh synthetic 4096-particle LJ argon, seed 1741, 2 fs, NVE,
120 K. It is **not** a protein/PME throughput or scientific accuracy benchmark.
Each successful retrieval checks every output's byte length and SHA-256, final
native step, and exactly one `grompp`. All inputs and native outputs are retained.

### Current r10 API / r6 workers / S2 0.11.20

| Test | Public operation | Placement | Native outcome |
| --- | --- | --- | --- |
| REST `rest-r10-a` | `421f95c4-b80e-5fc3-adfc-8d359233f25e` | RTX | 100,000 steps; 13 verified files; first attempt passed |
| MCP `mcp-r10-a` | `8497faa5-8de3-5a03-ae5e-ccd1618d8e4e` | RTX | 100,000 steps; 13 verified files; first attempt passed |
| REST `rest-r10-b` | `18d0732b-ba45-5b3f-bc96-1be36848679c` | RTX | 100,000 steps; 13 verified files; first attempt passed |
| MCP `mcp-r10-b` | `d4e00102-d35a-53fb-8386-c28bd7a06176` | RTX | 100,000 steps; 13 verified files; first attempt passed |
| Timed fallback `recovery-r10` | `57187f98-caac-55de-bebe-96341c761358` | RTX queue → L40S | 2,000,000 steps; 22 verified files; first executing attempt passed |
| Explicit recovery `recovery-r10-rtx-resumed` | `5ac8e921-0517-5dec-a306-8371a101877d` | RTX → L40S | 5,000,000 steps; 42 verified files; first L40S attempt passed |

The fallback run was originally intended for an RTX-loss drill. Both RTX slots
were occupied; S2 correctly selected L40S after its 60-second placement window.
The safety check refused to kill a non-RTX attempt. The job was allowed to
finish, and is counted as fallback evidence, **not** cross-GPU recovery evidence.
The separate `recovery-r10-rtx` job
(`914d5938-8dae-50d8-82b0-2d445efe4308`) reached step **462,300** on RTX before
the owned pod and its scratch were removed. Explicit resume restored on L40S
and completed, without a failed L40S attempt or repeated preparation. Native
GROMACS validated all eight trajectory segments: **5,001 frames, 0–10,000 ps,
no gaps or duplicates**. This is a new r10 result, separate from r9 below.

Both REST/MCP pairs ran unchanged API/worker/S2 images. The r10 RTX worker's
current/missing/forged identity checks returned 200/403/403; the fallback L40S
worker passed the same checks and terminal denial with its previously accepted
JWT returned 403. However, external Kubernetes access to the hub/control
clusters began timing out around 21:25 UTC. That blocked the extra r10 resumed
worker token capture and stale-JWT recheck, not native execution or public
REST/MCP. Do not claim that r9's live stale-JWT proof was repeated on r10.

### Measured startup, not estimates

Times below start at the successful submit response and end at the actual pod
start or first native command, not the poll that happened to observe it. MD
start is command `finished_at - wall_seconds`. Upload time is excluded (about
6 seconds for these tiny bundles); node acquisition, PVC and scheduling delay
are included. These are a few observations with existing nodes, not p95/SLOs.

| r10 test | Accepted → pod start | Accepted → preparation | Accepted → first MD | Accepted → success |
| --- | ---: | ---: | ---: | ---: |
| REST RTX | 67.5 s | 71.8 s | 91.2 s | 122.5 s |
| MCP RTX | 75.0 s | 79.7 s | 99.1 s | 131.0 s |
| REST RTX, second pair | 63.1 s | 68.1 s | 86.1 s | 118.1 s |
| MCP RTX, second pair | 64.4 s | 67.8 s | 87.1 s | 119.4 s |
| Fallback L40S | 129.5 s | 131.1 s | 141.1 s | 212.5 s |

The explicit r10 resume began MD **50.1 seconds after resume acceptance** and
finished 201.3 seconds after acceptance. Its first L40S pod began at 47.3 seconds.
Previous commands retained in the checkpoint are excluded from this restart
calculation; otherwise the original RTX command would produce a false negative
startup time. These remain synthetic runtime-recovery measurements.

Most latency is outside the small native calculation. Preparation/checkpoint
publication and cross-region object I/O are visible before MD. Cold node
provisioning and the earlier stopped RTX instances added minutes; do not quote
these warm-node numbers as a guaranteed scale-from-zero time.

### Earlier r9 identity/recovery and transfer evidence

- A 5,000,000-step RTX run (`a38cc048-3266-5dea-9cf6-e48b1f63d5b9`)
  was interrupted after an acknowledged native checkpoint at step 464,200.
  Its owned pod was force-deleted, the operation canceled, and old scratch
  deleted. Explicit REST resume on L40S created
  `e67565e8-4b68-5b69-8898-0e4f2cc1ebe9` and completed 5,000,000 steps,
  retrieved through MCP with 42 hashed output files and preparation only once.
- Current/missing/forged projected identity returned **200/403/403**. An old
  RTX JWT presented with the new L40S attempt ID returned **403**. A previously
  accepted identity could not obtain new object URLs after completion (403).
- Native GROMACS checked all eight trajectory segments: 5,002 raw frames,
  5,001 unique frames, complete 0–10,000 ps on the 2 ps grid. One restart boundary
  appears in both adjacent files. Native extraction proved the two frames
  byte-identical (`d2e787e2901ddd7d16792829c12b49cafe7e52c1316a522fb45b4d3f6f878a85`).
  The checker now permits only such an exact duplicate; it rejects missing,
  non-monotonic or differing frames. Raw files are unchanged.
- A live worker uploaded and read back **129 MiB / 135,266,304 bytes** through
  multipart broker URLs: upload 8.236 s, verification read 5.192 s, SHA-256
  `2a077b2a9a396ab450236593e9d85f323e0a06ff17ac05dab36f321c2b01a837`.
  This proves that broker path, not arbitrary multi-GB public API downloads.

### Failures retained, not reclassified as passes

- r6: two RTX VMs stopped during initialization; the existing fleet recovery
  controller restarted them. Fallback was then blocked by missing private input;
  active workers also exposed the JWKS User-Agent issue. All three r6 test
  operations were canceled and excluded from successful cohorts.
- r7/r8 container tags were built before refreshing the application wheel and
  were never deployed. Correct order is `uv build --wheel`, inspect wheel/image,
  build, pin, deploy. The sdist build cannot currently package the reused worker
  outside this component; it is not a supported distribution artifact.
- r9: the first L40S recovery attempt and another L40S job failed early with
  `PermissionError`; automatic same-class retries completed. The startup-status
  race is reproduced by regression tests and fixed in r10, but limited old logs
  do not prove that it was the only cause of those two historical failures.
- One r9 completed-artifact read returned 503 during the API rollout. Re-fetch
  after rollout verified all 13 files without resubmitting/recomputing. That
  cohort crossed an API change and is not an unchanged-release clean pass.
- The original trajectory assertion rejected the exact duplicate boundary
  described above. It was investigated using native GROMACS, not ignored.
- r10's orchestration script stopped at its extra Kubernetes identity inspection
  when the hub API TCP connection timed out. The already-resumed job continued;
  independent MCP polling and complete artifact/trajectory validation passed.
  The missing identity recheck is explicitly unqualified, not silently skipped.

## Remaining migration gates

1. **Automatic post-placement cross-GPU recovery:** explicit resume works;
   no automatic RTX-loss → L40S migration is claimed. Serverless must own it,
   not a second application scheduler. Timed fallback only covers unplaced jobs.
2. **Live durability faults:** denied final-object storage, lost final ack,
   partitioned stale worker and export-only recovery still need full live drills.
   Local failure tests and completed-result replay do not replace these.
3. **Customer data:** qualify actual external AWS/customer IAM, token rotation,
   provider revocation, scratch/log isolation and the approved protected-data
   path. The current API holds an internal test-bucket credential; GPU pods do not.
4. **Feature migration:** existing production keys/users, admin, usage/billing,
   batch fan-out, MPI and full legacy resume options remain on the old platform.
   The new result envelope is v2; input/artifact manifests remain v1-compatible.
5. **Performance and deployment:** realistic protein/PME runs, larger trajectories,
   cold provisioning, failure/recovery latency and a fresh Terraform
   create/test/destroy acceptance remain. No biochemical throughput claim is
   drawn from argon. Full application branch CI remains an integration gate.

## Cleanup and cost

All submitted r6/r9/r10 jobs are terminal: failed early r6 tests were explicitly
canceled, test-interrupted predecessors canceled, and all later scientific
continuations completed. At 21:29 UTC, no task pod remained in us-central1.
Two unused 20-GiB r6 PVCs were removed after matching operation labels, retained
cancellation receipts and verifying that no pod referenced them:
`op-0d5e1463afc1b177-work` and `op-d49e14bf4fcc8748-work`. Their deleted scratch
is not backed up; submitted inputs and all durable receipts/results are retained.
The exact old RTX scratch in both recovery drills was also deliberately removed
as part of qualification, not as an unrelated cleanup.

Hub/control Kubernetes connectivity recovered by 21:32 UTC, after the resumed
worker had finished. Matching the retained cancellation receipt and operation
label allowed removal of the remaining owned 20-GiB r6 fallback PVC,
`op-e6b3a04b4eede467-r1-work`. Total cleanup: **three failed-test PVCs, 60 GiB**;
unrelated `op-container-run-*`, shared scratch and shared weights were untouched.
At 21:33 UTC, no test pod remained in either GPU namespace; both API images and
the pilot health response were rechecked. The four auto-provisioned GPU nodes
were still Ready/idle, so node scale-down itself is not claimed as verified.
Their existing min-0 autoscaling owner remains responsible; no manual pool
floor or bound change was made. The missed r10 stale-JWT recheck remains a
qualification gap even though operator connectivity recovered.

For the r6-onward interval through 21:30 UTC, an approximate **GPU-only upper
envelope is USD 2.15**: two RTX GPUs for 38 minutes at the handoff's USD 1.09/h,
one L40S for 36 minutes and another for 18 minutes at USD 0.85/h. This is not an
invoice or total fleet cost: it excludes CPU, disk, traffic, earlier r5 work and
unrelated fleet lanes; actual stopped/idle/autoscale intervals affect billing.
Operation `cost`/`gpu_seconds` remain null in these receipts, not measured zero.

The pilot API intentionally remains available for continued testing. Retain
private receipts, input/output objects and checkpoints; clean only exact owned
temporary compute/scratch after checking no active attempt references them.
Both task-used spot pools keep their existing min 0 / max 2 bounds. Do not change
other fleet lanes' GPU floors or the customer cluster. Private receipts live in
the operator directory documented in [README.md](README.md); no tokens, provider
credentials, Terraform state or signed object URLs belong in Git.
