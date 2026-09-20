# Performance collection deployment — 2026-09-20

Status: installed and collecting, **not a completed model/GPU qualification**.

## Retained service

- Repository: `rene-tech/nebius-solutions-library`, branch
  `agent/fs2-performance-placement-r20260920`.
- Project `project-e00rene`; cluster `mk8scluster-e00j5z9te7x5dd9g6a`;
  namespace `fs2-system`; Helm release `fs2-serve-control-plane`, revision207.
- Initial advisory control plane source `5bba10c360a7de54424c43b3ad649715ddcc8bbd`, image
  `sha256:34b45d61e3817d8cf31ca102bc18a2ac8445daf8f7dfc4b1193386aa5fc55738`.
- Sibling206 subsequently integrated that source into `728b48d92` while adding
  reference-model contracts. Current backend image
  `sha256:123f169cb337f24b35a71fad8adf88acbc2c1568b0415e3f465412c5899a552a`
  was preserved unchanged by the worker-only207 upgrade.
- Four CPU workers: source `463fbe7ff9a71d23ed236169cff27ec8865815d3`, image
  `sha256:d534cdadf42915a4d0f1d3676fd224662b11141253621ea5e83c2f664b84331d`.
- Admin image remains
  `sha256:b8f5a588a57ff51bbe5cf8ab2ce5176cafcde22b76ac61c819fd86aea8db31a5`.
- Registry prefix: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform`.
- UI: <https://89.169.99.188/admin/capacity/benchmarks>.
- API: `/admin/api/v1/performance/campaigns`; observed hardware API:
  `/admin/api/v1/performance/hardware`. Both use existing operator sessions.

PostgreSQL migration0035 is the durable authority. Existing Kueue, KEDA,
JobSet and model controllers retain scheduling/replica ownership. No node group,
GPU quota, customer admission limit or customer placement policy was changed.
The worker's 4 GiB memory limit is for CPU-side full dataset validation.

## Campaigns

| Campaign | ID | Scope |
|---|---|---|
| Initial | `b66e8512-ec63-4586-9309-491c65b0a79f` | 40-entry catalog capture, 25 executable models ×3, 15 explicit initial exclusions |
| Extension | `89f7966c-af12-443d-a2e6-666a028f573b` | 11 additional models ×3 |
| Robotics | `83d4fd91-388e-4469-9634-7429f731a551` | Cosmos Transfer and LeRobot augmentation ×3, parallelism1 |
| Corrected | `fc4c12bd-53a9-45d3-87e8-5ac12508bff9` | Cosmos Nano, DiffDock, GenMol and BoltzGen ×3, parallelism1 |
| Music | `00cd3196-fc64-4d05-bcc9-08080e0fa355` | Newly published ACE-Step, two12-second instrumental clips ×3, parallelism1 |

Together these cover 38 distinct models from the captured catalog. GLM is
excluded on this H100/L40S/H200 cluster; the remaining entry is a dated test
clone, not a distinct model. ACE-Step was added by a sibling release after
the inventory capture and has its own extension campaign. Two newer reference
LLM canaries are outside the frozen initial cohort; they are not silently
counted as benchmarked.

Transfer2.5 is restricted to the private `video-canary-20260920` principal.
The benchmark identity received403; its publication/permissions were not changed.
The207 worker skips later requests in a cohort after a recorded401/403 and
records `access_denied_not_retried`, without a fabricated latency or GPU result.
The access decision was sent once to Rene in Slack. Other campaigns continue.

Initial failed trials are immutable. The corrected campaign fixes runner bugs
(Cosmos operation name, externalized results, finalized-upload replay and
RDKit availability). MolMIM's `generation_exhausted` is a real request outcome,
not one of those runner corrections. The two explicitly cancelled, benchmark-
owned BoltzGen queue orphans remain recorded. A third retained operation
`3fc88040-fa50-4c8b-a51c-d2243adf3161` completed successfully with server semantic
outcome `passed`; it does not retroactively change its failed runner trial.

## Verification

- 286 integrated PostgreSQL/API/worker/Helm/model-contract tests passed after
  merging the exact preceding ACE-Step release `f7d3f5140`.
- Earlier admin production build and five UI tests passed. The live authenticated
  Capacity → Benchmarks screen was exercised with both original campaigns:
  selector, outcome denominator, profiles and null timing display; no console
  errors during the authenticated session. The test session was signed out.
- The exact worker image passed its CLI import test and full LeRobot0.6.1
  validation with non-root UID, read-only filesystem, no network, one CPU and
  a 4 GiB memory limit. The earlier 2 GiB OOM result is retained, not concealed.
- Revision205 deployed successfully with all four new workers running. Public
  operator API checks verified advisory output, 24-node hardware inventory,
  41-entry updated model inventory, Apps and Capacity summary. The advisory
  correctly returned `more-evidence-needed`, with no preferred GPU or mutation.
- An API-backed snapshot after rollout contains47 validated trials across28
  models,33 hardware observations and28 runtime/startup observations. These
  counts are a dated progress snapshot, **not the final campaign result**.
- The207 worker-only rollout is deployed with4/4 workers. Eleven runner/music/
  export tests and37 final integrated performance/reference-contract tests
  passed. The exact published image passed its import/CLI test. The scoped
  local PostgreSQL test container was removed after testing.
- A later post207 snapshot contains68 validated trials across34 models and53
  hardware observations. Five campaigns are retained, including music. They
  have not all finished; the Task Deck card remains running.

## Evidence and continuation

`report.py` exports the current durable APIs, not a handwritten results file.
Each terminal trial points to a SHA-bound artifact receipt. Preserve the source
SHA, fixture SHA, exact operation, runtime image, node identity and all failures.
Do not pool client recovery timings (null), estimate absent startup phases,
or call the uncontrolled-cache baseline a cold-start/snapshot benchmark.

Protected local captures are under `/home/tux/secure-handoff/`:
`fs2-performance-release-20260920`, `fs2-performance-final-rollout-20260920`,
`fs2-performance-report-20260920-r205`, `fs2-performance-report-20260920-r207`,
`fs2-performance-access-aware-rollout-20260920`, and the five campaign directories.
The database and artifact service, not the workstation, own the results.

Next: supervise all captured-model trials to terminal results; investigate
model/request failures separately from capacity waits; retain full speech
quality observations; finish the ACE-Step extension; then schedule isolated
compatible-GPU and controlled warm/cold/snapshot
cohorts. Cross-GPU recommendations must stay unavailable until their comparison
requirements are met. No customer model eviction is authorized by a benchmark.

The dedicated `platform-benchmarks` identity expires on September21 and retains
concurrency4/request-budget2000. Its Secret is
`fs2-performance-benchmarks-20260920`; credentials are not committed here.
Revoke/disable benchmark workers after collection is finished, or deliberately
rotate the benchmark identity before another campaign. Do not change limits to
work around contention. Retain completed artifacts, receipts and usage records.

The task remains running in Agent Task Deck:
`fs2-performance-placement-platform-r20260920`. Controlled cross-GPU and
cold-start collection, full-catalog completion and final recommendations remain
open. The scoped benchmark worker authority follow-up is tracked independently
by the existing security integration program; this rollout does not claim
completion of that program.

Additional observed collector gaps: a Proteina trial returned
`execution_identity_mismatch`, and another polling attempt received503 while
its underlying scientific operation continued. Those are retained failures,
not evidence that no GPU work occurred. Reconcile the expected execution
contract and reanalyze the exact existing operation before any corrective
resubmission. Read-only status polling can safely gain bounded transient-error
retries; do not extend that to ambiguous POST admission retries. Simultaneous
sibling backend rollouts are recorded context for these exploratory cohorts.
