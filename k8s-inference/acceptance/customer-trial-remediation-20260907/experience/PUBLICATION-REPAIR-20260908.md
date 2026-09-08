# Terminal result publication repair — 2026-09-08

Status: offline implementation and tests passed; awaiting the root-owned release and coordinated live acceptance. This is not a new successful customer cohort.

The shared worktree base is `984cf72f36b3c676fe2c6faa1fc5c5722d4f983d`. The production change is confined to `ScientificRunDetailPage.tsx` and its test. A controller-successful run continues the existing five-second query interval while semantic validation is `not-run`, with an explicit **Finalizing results** message. Polling stops after result publication (including a published semantic failure), or an explicitly failed/cancelled run. An empty artifact list alone does not keep a published result polling.

## Offline evidence

- Full admin UI suite after the approved startup-retention addition: 164 tests passed across 24 files (8.02 seconds).
- TypeScript type checking passed.
- Focused detail-page suite: 12 tests passed.
- Acceptance helper tests: 10 passed, including three automatic-publication checks.
- The deterministic component regression advances from active to controller-successful/unpublished, then to published output, without navigation. Advancing another 20 seconds produces no additional detail query.
- Node helper syntax and `git diff --check` pass.

No model traffic, browser session, policy mutation, deployment, commit or branch was created by this offline verification.

## Optional startup-retention control

The separately authorized form addition exposes `availability.startupTimeoutSeconds`: an optional integer from 60 to 7,200 seconds. Omitted or null values display the effective 900-second default without adding an override to the draft or request. An actual edit writes the setting; **Use default startup retention** removes it explicitly. The help text separates startup retention from idle/cooldown timers and states that the hot floor is unchanged.

This addition touches `modelDeploymentTypes.ts`, `lib/modelDeployment.ts`, `ModelDeploymentForm.tsx` and their helper/workspace tests. Thirteen new cases cover omission/null preservation, integer/range validation, explicit setting/reset, and unchanged runtime and other availability fields. Existing fixed-hot Pod templates are not edited by this UI change. The backend/renderer behavior and unchanged existing digests are owned and tested in the observer lane.

## Post-release acceptance

Root must first confirm the exact deployed source and healthy rollout. Use a fresh private output directory and the existing in-memory credential helper; do not overwrite any earlier attempt.

1. Open a newly running RFdiffusion or Protenix detail page once. Let its own queries advance the page; do not navigate again or manually refresh to recover results.
2. After the page shows published results, send the existing browser helper `{"action":"verify-publication","label":"protenix-automatic-publication"}`. This checks the recorded real UI queries, one successful navigation, completed semantic validation and authorized output availability.
3. If the real polling samples include the controller-successful/unpublished interval, retain its timestamps. If the interval falls between polls, record that it was not sampled; the helper does not manufacture race-window evidence.
4. Use the helper's download action to click the actual authorized artifact link and verify bytes and SHA-256. Retain the screenshot and redacted proof.
5. Leave the terminal page open beyond the five-second interval, take another snapshot and verify no new detail queries were made. Close the owned browser and export its observations at the coordinated cohort boundary.

Two consecutive clean unchanged customer cohorts remain a parent-level acceptance requirement; this page test alone cannot satisfy it.

## Historical browser failures

The original blank-page preflight and three later `ERR_NETWORK_CHANGED` read failures remain preserved and unexplained. A subsequent bounded diagnostic loaded the expected HTML/JavaScript/CSS and sign-in page correctly. Current session initialization has explicit loading, unauthenticated and failed-session views; the retained evidence does not identify a persistent initialization defect. Console and failed-request capture are retained for the next live attempt. No speculative production workaround was added, and the original failure is not reclassified as harness-only.

See [the original terminal publication race](TERMINAL-PUBLICATION-RACE.md), [r02 experience](R02-EXPERIENCE.md), and the unchanged [r02 browser observations](r02-browser-observations.json).
