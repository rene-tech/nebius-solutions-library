# Real late-state demo continuation — 2026-10-05

Status: the authorized customer checkpoint has been copied and byte-verified;
local fixture/runner tests pass. No demo GPU operation has been admitted yet.
The final signed-handle renewal release and measured L40S recipe are prerequisites.
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

27 focused fixture/science/owner/export tests and Ruff pass. Tests reject changed
bytes, duplicate/unsafe paths, wrong source state, missing topology comparisons,
unapproved TPR differences, incorrect start step, customer-key substitution and
corrupted/missing/foreign-bucket output objects. The full native comparison parser
also consumed the independent exact-runtime L40S comparison log successfully.
Actual final-release public runs and their sustained performance remain pending.
