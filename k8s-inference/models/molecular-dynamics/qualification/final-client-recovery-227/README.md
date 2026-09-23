# Final-client recovery on Helm 227

All eight existing canonical operations—two per engine—were freshly recovered
with exact client `81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`
against the deployed Helm 227 surface. This is **artifact readback**, not eight
new simulations. Original scientific runs and their qualification remain bound
to Helm 226.

The [sanitized receipt](receipt.json) records **424 artifacts, 2,749,521,423
artifact bytes and eight manifests**. All 432 content downloads were fresh
`verified-copy` transfers, with one attempt each. Every file was independently
rechecked for size and SHA-256; every manifest is byte-identical to the original.
No admission, input upload, cancellation or GPU execution was requested.

| Engine / selected protocol | Cohorts | Artifacts | Artifact bytes | Content downloads, including manifests |
|---|---:|---:|---:|---:|
| AMBER 02/03, canonical SCR/LFMiddle | 2 | 78 | 387,883,962 | 80 |
| NAMD, explicit PME64 01/02 | 2 | 154 | 216,041,690 | 156 |
| GROMACS 06/07, fixed-cutoff `-notunepme` | 2 | 112 | 238,789,960 | 114 |
| LAMMPS, canonical fixture 04 01/02 | 2 | 80 | 1,906,805,811 | 82 |
| Total | 8 | 424 | 2,749,521,423 | 432 |

The final client source is `88109af67d11726d1e07e14c5d0ff4e83a4837e0`.
Its installed API helper hashes to
`3d40bb466778ac12244421f889899122f1c7a73a5415e910d25dc0adda165f4d`;
the receipt library hashes to
`d006441697bbc1c9941daba5f7e6e9ea19ffc4798354a5cf3f83cdef5ecfae56`.
The bounded recovery entry point and streamed GET path were inspected directly
inside that image. Fresh-copy records plus this code establish the content GET
claim; no packet capture or total MCP RPC count is asserted.

[Independent live observations](live-surface.json) confirmed Helm 227,
control-plane image index
`719ec336ef93e582f3031735dba974b61ea97f1c4ce47e630fe18eae9e9cd77c`,
three ready API replicas, two ready controllers and two ready admin replicas.
The post-read observation at 19:37:23 UTC shows zero restarts on those seven Pods.
API limits remain 2 CPUs / 2 GiB. Source provenance and readiness by 19:24 UTC
come from the rollout owner; all readbacks began after 19:25 UTC. Runtime image
identities were independently observed during and after the campaign, not traced
per individual HTTP request. This task changed no deployment or resource limit.

Each operation used its original ordinary owner key. Fingerprints were compared
locally before execution; neither fingerprints nor credentials are published.
Fresh private directories are named `final-client-227-<engine>-01/02`. LAMMPS
was recovered by the parent; its complete downloads were independently rehashed
again while assembling this report. The other six recoveries ran in this task.

The reusable [recovery helper](../recover_image_customer_results.py) and its
14 tests preserve owner identity, refuse a nonempty recovery directory, verify
all published outputs and expose no submission arguments. With the portable
analysis gate, 35 focused tests pass. The separate
[portable analysis/render qualification](../portable-four-engine-analysis-20260923/README.md)
also passed on this exact client without network, GPU or API credentials.

Earlier readbacks remain historical. Superseded autotuned GROMACS 04/05 and NAMD
PME44 are not selected here. Original numerical/ensemble caveats and LAMMPS's
explicitly verified SHAKE restart-boundary setup remain in the original science
receipts; artifact recovery neither changes nor expands those scientific claims.
Successful first-attempt reads do not independently test retry-on-failure or
browser UX, and are not a whole-platform readiness declaration.
