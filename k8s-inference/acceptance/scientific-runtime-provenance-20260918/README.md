# Scientific stage runtime provenance — exact release170

Source `1ad44fce044a783398bb5a7cbbdad28be4a8e08e`, deployed as
`fs2-platform/fs2-serve-control-plane@sha256:2172e19536a3d61d6fc546fd50c9ccdf5658b072adc7edfaa92435f6c8405307`
in `project-e00rene`, `eu-north1`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`.
Previous image: `sha256:6c8b9f8701cfbcde4ea64c180750e9969303b5aa03bcb837edf7ede3401dcad2`.

## Defect and scope

The existing lifecycle ledger recorded node and GPU identities, but the
scientific artifact bridge copied only Pod IDs into terminal attempts. The fix
projects existing operation/attempt-scoped facts, filtered to known attempt Pod
IDs and job identity. It creates no new tracking policy or fabricated history.
Missing observer evidence stays absent. A multistage parent is not represented
as one flat serving Pod; inspect its result attempts/admin stage inventory.

Five direct regression cases cover CPU/GPU, missing observations, multiple Pods,
scope mismatches and idempotent closed attempts. The broader focused command
passed129 tests with four PostgreSQL-environment skips; another lifecycle and
production integration selection passed82. These selections overlap and must
not be added into a unique-test count.

## Ordinary public GPU verification

Scientist09's ESMFold2 request `esmfold2-1tim-pdb70-depth64` completed as
`cf771dd7-ac0f-42d9-bd09-8b6497270c01` in114.767391s. The original pinned PDB70
MSA/input manifest was reused for this explicit regression, not counted as a
new scientific study or paper reproduction.

| Stage | Attempt | Actual node UID | GPU UUID | Admin node/GPU counts |
| --- | --- | --- | --- | --- |
| prepare-input | 3656cfd3-787a-52bb-a7c7-b03c1b093ab5 | 70979104-26b3-4fbc-a1bd-677d099793c4 | none, CPU stage | 1 / 0 |
| fold | 80de42bf-e442-58d8-91f5-fb54c16110f8 | 5700ea19-0b46-4020-afa5-c8f1a105dcdd | GPU-eb9c2898-bf6f-83b6-b76c-2c3b5b02500c | 1 / 1 |

The GPU stage ran on H100 node `computeinstance-e00p3acr87k9k4mckj` using the
existing reserved pool; its CPU stage ran on the existing CPU pool. No new
capacity, model settings, credentials or limits were required by this test.
Terminal artifacts, stage accounting, public events and independent existing
lifecycle correlations are retained under protected campaign evidence:
`cohorts/scientific-runtime-identity-r1/scientist-09/esmfold2-1tim-pdb70-depth64/`.

The same release repairs exclusive-end Loki pagination. A fixed historical
window's500-row read exactly equals the first500 rows of three200-row pages.
Read latencies were0.888s single and0.758/0.875/0.918s paginated. Existing8s
timeout and5000-line ceiling are unchanged. Full protected receipts are in
`provenance-cursor-optional-release170/public-log-pagination/`.

## Release caveat

Two already-deleting GPU-observer Pods on provider-confirmed STOPPED instances
stalled the DaemonSet rollout. Their exact manifests/provider states were saved
and only those obsolete Pod records were removed; nodes/customer jobs were not
deleted. All13 eligible observers then reached the new image and Ready state.
Helm170 completed within its unchanged10-minute atomic deadline. This manual
recovery is retained as an operational gap, not an automatic recovery claim.
The four existing scientific snapshot registrations and other serving owners
were preserved. Separate Cosmos owner changes and tests have their own receipts.
