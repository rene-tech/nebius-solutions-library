# Final cross-lane consistency review

Reviewed 2026-09-15, approximately 23:31–23:36 UTC, for
`fs2-bioir-report-r20260915`. One bounded read-only review of the consolidated
report and all four model lanes / four snapshot-model studies. No experiments,
cluster operations, source-lane edits or promotion were performed. The reviewer
authored the OpenFold lane; this is consistency checking, not independent
experimental replication. Root remains final acceptance owner.

## Material publication findings — resolved

1. **Refresh the aggregate after the final source freeze.** The inspected
   [comparison.json](comparison.json), generated at 23:33:45 UTC, had all twelve
   required models and all four snapshot models, but its embedded Boltz cleanup
   and Protenix snapshot cleanup/resource-closure/prior-cohort records differed
   from the newly finalized lane JSON. These are closure updates, not changed
   timing/quality measurements. **Resolved:** the 23:35:31 UTC regeneration was
   checked at 23:36 UTC; all model entries exactly match final lane JSON (apart
   from the added source pointer), and all three embedded snapshot documents
   exactly match their sources, covering all four snapshot models.
2. **Close the historical storage follow-up in the snapshot summary.**
   [snapshot/report.md](../snapshot/report.md) still described the former Boltz
   cache-PV CSI timeout as an open follow-up. The
   [final cluster receipt](final-cluster-state.json) explicitly records that PV
   absent at 23:31:26 UTC. Preserve the historical failure and append a dated
   closure note. **Resolved:** the snapshot report now includes an explicit
   “Final closure supersedes the historical residual” section linked to that
   receipt; no storage follow-up remains open. The Boltz report also preserves
   the historical failure and confirms closure.

No material contradiction was found in the consolidated numerical claims,
scientific-quality warnings or adoption holds. No outstanding material review
finding remains. Resolution changed publication/closure metadata only, not
experimental scope or raw measurements.

## Checked

- [x] Twelve unique models: eleven frozen BioNeMo catalog entries plus Protenix.
  OpenBind remains a separate applicability result. Eight unsupported full-model
  BioIR paths retain N/A gains, not invented zero/one-times comparisons.
- [x] Same-GPU ratios, residency controls and precision-changing variants are
  distinguished. Reported medians agree with lane summaries; n=3 timing repeats
  are not presented as tail-SLO or broad scientific noninferiority evidence.
- [x] Denominators are explicit: Boltz 228 completed attempts / 191 artifact-valid
  predictions versus 218 sampled CIFs; OpenFold core 312 artifact-valid outputs;
  Protenix 66 attempts / 65 artifact-and-sequence-valid predictions. Expected
  HTTP error probes, startup failures and interrupted cohorts are separate.
  Coverage allocation-per-product-result divisions match their stated counts.
  MolMIM has zero newly decoded results and undefined cost per new result;
  Complexa's nine workflow artifacts are explicitly distinct from zero native
  scientific-success rows. MSA search allocates no GPU.
- [x] Boltz's ten A/C→A/B chain-ID failures remain a failed integration gate;
  sequence validity and promising CA-lDDT do not override that gate.
- [x] OF3 unique-ID graph timings are potential only. Three ordinary controls
  reproduce the reused-key fault after valid warmup; three restored processes
  also fail. Six first-fault events and twelve poisoned follow-ons are not
  eighteen independent kernel failures. Seven valid artifacts / 25 snapshot
  attempts, no restored-output parity and no valid-result startup ratio.
- [x] Protenix's 18-structure native-batch reference CA-lDDT medians
  0.63250→0.49223 support the quality HOLD. Final y0jt snapshot data are separate
  from fkt/xjaw: 3 restores / 3 controls, 88.909 s / 57.820 s readiness, 18 valid
  predictions, no numerical/scientific parity claim or first-output speed ratio.
- [x] Protenix fallback is n=1, 13.421 s, with a new worker PID and zero restore
  commands. Its donor executable cache differs from the three cold-cache normal
  controls. No matched cache-only speedup or causal attribution is claimed.
- [x] Boltz/OF2 snapshots restore correctly but are slower (93.60/40.04 s and
  98.23/30.89 s); limited paired text equality does not transfer to OF3/Protenix
  or to different GPU identities. Cross-node portability remains untested.
- [x] Final cluster receipt shows no evaluation namespace objects and all
  27 model deployments at desired readiness. The later j20 STOPPED/NotReady
  incident is separately documented in [its timeline](capacity-incident-j20.md),
  after the completed matrix and GPU-idle cleanup receipt; it is not substituted
  as the cause of earlier repeated-key application faults. No promotion is claimed.

GPU-performance skill criteria guided the timing/quality/cache-state separation;
Task Deck instructions kept this review bounded to the assigned report task.
Manager acceptance and publication remain separate from this review.
