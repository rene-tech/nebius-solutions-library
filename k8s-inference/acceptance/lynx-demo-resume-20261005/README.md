# Real late-state demo continuation — 2026-10-05

Status: the short demo-owned API continuation has passed its native, history,
postprocessing and full platform/customer-bucket byte checks on the exact deployed
release. The separately measured sustained 65-ns cohort is now admitted; its
six-hour/native and 200-ns/day delivered gates have not yet passed.
This directory is acceptance tooling, not a customer-specific serving service.

## Exact test scope

The original six-hour source is generation 71, native step 13,963,440
(27.92688 ns), with 305 files / 82,674,112 bytes. Its input describes 185,486
atoms, a 2-fs timestep and a 500,000,000-step / 1-µs target. Source TPR SHA-256:
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.

The operator's `prepare_demo.py` copies and verifies the exact committed files
into the existing demo user's private bucket prefix. It does not write the source
bucket, use a customer inference key, replace starter data, or change a quota.
Credentials and all molecular bytes stay outside Git under
`/home/tux/secure-handoff/fs2-lynx-demo-resume-20261005`.

`prepare_acceptance.py` independently rehashes every copied file and builds a
deterministic gzip-tar. All original files, including old wrapper logs and native
checkpoints, live unchanged under `source-history/`; new runtime files cannot
overwrite them. `convert-tpr` writes a separately hashed finite acceptance TPR.
Full native `gmx check -s1/-s2 -tol 0 -abstol 0` must report only the intended
`nsteps` difference, covering topology, interactions, exclusions, box, coordinates
and velocities. No velocities, timestep, force field, accuracy or output cadence
are regenerated or silently changed.

GROMACS 2026.2's [checkpoint reader](https://github.com/gromacs/gromacs/blob/v2026.2/src/gromacs/fileio/checkpoint.cpp)
computes remaining work from the original `init-step + nsteps` endpoint minus the
checkpoint step. With this input's initial step zero, another 60 ns means an
absolute endpoint of 43,963,440, not 30,000,000. The supported
[convert-tpr interface](https://manual.gromacs.org/2026.2/onlinehelp/gmx-convert-tpr.html)
changes only the separately identified test horizon. The deprecated `mdrun
-nsteps` override is neither exposed nor added to the managed worker contract.

## Public acceptance sequence

1. Select the independently measured, publicly expressible L40S tuning. Prepare
   a bounded 2-ns cohort, then a long horizon exceeding six hours at the measured
   upper speed. Never extend the horizon after admission or count a faster run
   shorter than six hours as passing the soak-duration gate.
2. Submit the actual late checkpoint/history through ordinary demo-owned REST
   `run-workflow`, using `restart_checkpoint`. A deliberately short 300-second
   bootstrap budget establishes a same-owner committed checkpoint. Its clean
   `WORKFLOW_TIME_LIMIT_EXCEEDED` result is labelled fixture setup, not a customer
   production failure and not the long qualification.
3. Verify original history, complete TPR equivalence and native start at the
   original saved step. Invoke the literal demo-owned `:resume` endpoint with
   its existing fourteen-day maximum. Replays must return the same operation.
4. Preserve observations at each generation, terminal semantic validation and
   final native step. Compute newly completed nanoseconds divided by the entire
   API-accepted-to-durable-terminal wall time, not the headline native counter.
   The long gate requires at least 200 ns/day delivered and at least 21,600
   native execution seconds. Report the short cohort's overhead honestly.
5. Download and SHA/size-check every final platform artifact, then independently
   GET the customer-bucket manifest and every unique exported object. Retain
   retries, failures, exact release/placement, readiness and cleanup evidence.

`run_demo_resume.py` only accepts the resolved existing `demo-user` owner with
the scoped two-slot policy. It changes no key or policy. Its saved state reuses
operation IDs/idempotency keys if interrupted; it never resubmits a new study to
hide a failed operation. Root coordinates production releases and capacity.

The accepted simulation budget is not a fourteen-day soak. A clean finite
continuation establishes only the duration, recovery and workload actually tested.

## Customer handover distinction

The real customer's current recovery keeps running and has progressed beyond
generation 71. It must not be cancelled or reverted to this older acceptance
fixture. The generic customer handover lives in
[`../lynx-customer-handover-20261005`](../lynx-customer-handover-20261005): select the
latest committed state after the customer's chosen stop/failure, preserve the
full original target, and submit an explicitly tuned native import. Ordinary
`:resume` preserves old arguments; it does not itself enable new performance
flags. No cross-tenant database ownership rewrite is used.

## Local evidence

The real source has 70 trajectory parts, of which 42 are zero bytes: its sparse
1-ns coordinate cadence does not emit a frame in every five-minute wall segment.
The pinned native reader aborts when `trjcat` receives one of those empty files.
This is a real postprocessing failure, not a reason to change scientific output
cadence or discard history. The fixture now explicitly requests
`{"files":"continued.part*.xtc","nonempty":true}` for `trjcat`, and the matching
energy selector for `eneconv`. Both single-GPU and MPI schemas expose the same
optional selector; absent/false retains the old behavior. Every original file,
including empty ones, remains in checkpoint and output validation. Unmatched or
all-empty selections fail before invoking a native reader.

134 focused runtime, schema, MPI and acceptance tests pass, including explicit
selection, legacy normalization, all-empty failure, path validation and all four
public schema projections. This is not a substitute for the new-image native
checks or final public demo continuation.

Exact worker `sha256:ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`
passed the CPU-only `qualify_empty_segments.py` regression in 3.87 seconds:
the unfiltered 70-part control reproduced the native assertion; the explicit
selector joined all 28 nonempty parts and `gmx check` verified 185,486 atoms,
28 frames and unchanged 1,000-ps cadence. The joined trajectory is 19,424,220
bytes, SHA-256 `e836ed0dc4daf5b3f35e8d8fc6eac674190a83ed01449d7b1ac12752f1a5ef2e`.
All 305 source files were rehashed unchanged. Detailed retained evidence:
`/home/tux/secure-handoff/fs2-lynx-demo-resume-20261005/nonempty-native-r1/`.
The short public fixture uses 2 ns, list 200, pin auto, 8 threads;
its measured delivery rate is reported without the long cohort's 200-ns/day
or six-hour threshold.

33 focused fixture/science/owner/export tests and Ruff pass. Tests reject changed
bytes, duplicate/unsafe paths, wrong source state, missing topology comparisons,
unapproved TPR differences, incorrect start step, customer-key substitution and
corrupted/missing/foreign-bucket output objects. The full native comparison parser
also consumed the independent exact-runtime L40S comparison log successfully.
## Exact deployed release and public evidence

Deployment source `2f7c2d1e3`; API/tools/controllers/maintenance:
`sha256:1c22336993e588408c069b5a8f93e550ea60829f167099f55acdc1f3918fd0d9`.
The single-GPU worker is `ca863f44…` above; the MPI wrapper is
`sha256:c10a9b2edfab1678153ca45c05c00e4649979065ef2ecadfb69a17b83483f662`.
Execution ConfigMap `fs2-r927c465c6d-scientific-execution-045f5a4de9ee`;
scheduling remains the unchanged `d45` revision. The deployment owner retained
these immutable digests in the current Terraform tfvars and verified formatting;
no unrelated Terraform apply was performed. All three API readers, both
controllers and a new maintenance Job passed before admission; nine MPI shapes
were advertised. Release evidence is under the private root's
`nonempty-reader-verification/`.

Short bootstrap `bbfce68c-e9d9-4bea-a655-97a13fffddb6` was accepted at
20:23:48 UTC; initial/replay latency was 0.321/0.288 seconds. The compressed
48,461,740-byte input upload took 4.634 seconds. Actual default placement is one
L40S on the AMD four-GPU pool, node `computeinstance-e00xwjv9khjp8fhp3v`;
the customer workload on that node was not modified. This is a separate hardware
measurement from the final native-only Intel L40S confirmation.

The deliberately exhausted 300-second bootstrap budget saved native step
14,312,400, advancing 0.69792 ns from the copied checkpoint. Its native process
reported 216.351 ns/day over 283.518 seconds and exited 1 at the intentional
budget stop. This is expected fixture setup, not a failed sustained segment.
All 305 source-history identities remained present. The first validation pass
stopped because stderr's closing quotation interrupted the word `comparing`;
the strict harness parser was corrected to reconstruct only that exact block.
The real TPR comparison then passed every required section with the sole `nsteps`
difference. No scientific tolerance was relaxed and no bootstrap was resubmitted.

Literal same-owner `:resume` accepted
`fcee676b-63d9-41c6-9248-3e30f0c5d557` at 20:32:07 UTC; initial/replay latency
was 0.770/0.512 seconds. It succeeded durably at 20:41:35.398 UTC, reaching step
14,963,440 with both native segment exits zero and all requested postprocessing
successful. The two segments reported 222.320 and 216.142 native ns/day. Only
newly resumed work counts: 1.30208 ns over 521.286 native process seconds and
567.684 seconds from API acceptance to durable completion, or **198.173 ns/day
delivered**. This short run does not meet the long run's 200-ns/day threshold;
its real startup/export overhead is included rather than discarded.

All 331 final platform files (131,295,291 bytes) passed independent SHA/size
verification in 24.103 seconds. The customer export independently passed all
331 paths / 288 unique objects (118,496,704 bytes) in 10.413 seconds. There were
zero extra download attempts, all 305 original source files remain preserved,
and final three-reader readiness capture found no non-200 probes or coverage
gaps. The first 318-file checkpoint handoff took 7.499 seconds, well below the
unchanged 600-second bound. Receipts are in `short-r1/`, including a separate
strict accounting recheck that rejects imported/bootstrap timing history.

The subsequent 65-ns fixture targets step 46,463,440, with unchanged science and
explicit nonempty postprocessing. Its bootstrap operation
`01954a58-69b4-4375-8133-4358e85ee48b` was admitted around 20:46 UTC using the
same demo-owned API; input upload took 5.226 seconds. The six-hour/native and
200-ns/day delivered gates count only the new resumed operation after the
expected bootstrap stop, not bootstrap time or work. Timestamps and operation
identity reject imported timing history. Sustained results remain pending.

Read-only attempt, hardware and exact-reader readiness observations are retained
under `short-r1/` and `short-r1-readiness/`. The long run uses the same bounded
observers under `long-r1/`, with consecutive windows of the existing readiness
sampler in `long-r1-readiness/` throughout the cohort. Native five-minute
segments are not reported as retries. There is no sustained-readiness claim
from the short functional result alone.
