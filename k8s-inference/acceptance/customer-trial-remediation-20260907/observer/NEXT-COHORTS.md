# Observation protocol for r02 and r03

Prepared only. **Do not start traffic or deploy from this document without root's explicit cohort START.** Root integrates the Qwen idle-acknowledgement and admin phase-duration fixes, builds and deploys first. The first successful scientific cohort r01 is preserved but does not count as clean overall acceptance.

## Fixed scope

- Same H100 cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, region `eu-north1`; explicit context `k8s-inference-h100` and private kubeconfig below.
- Same 14 scientific fixtures, nine model/profile variants, RF→Protenix→Mosaic switching, then 11 mixed operations with at most four clients, including four-shard low-priority RF bulk. The workload lane owns submissions; observer creates no extra scientific requests.
- Same ordinary Qwen sampler: one client, one request every 25 seconds, alternating HTTP/MCP, unchanged semantic checks and no submission retries. Do not mix standalone burst-regression traffic into these measurements.
- Same 25-second cluster sampler and existing resource ceilings. Only the two existing per-Pod CPU/RAM queries now include `fs2-academic-poc`; r01's missing academic Pod metric scope is explicitly documented, not retrofilled.
- No live policy, minimum-hot count, image, quota, driver, scheduler, node limit or capacity-reservation changes during either cohort. Normal existing autoscaling and Kueue preemption remain enabled. No forced node scale-down on completion.
- Do r02, finalize its evidence, and receive root's r03 START before running r03. If a repair/deployment becomes necessary, preserve the failed cohort and restart the two-consecutive-clean count after the final deployed fix.

## Paths and startup

Use a fresh cohort ID and output directory every time. Never copy r01 stop/phase/completion files or use the interactive sampler's resume option for a new cohort.

```bash
TRIAL_CODE=/home/tux/nebius-solutions-library-inference/k8s-inference
TRIAL_PYTHON=$TRIAL_CODE/components/control-plane/.venv/bin/python
TRIAL_ACCEPTANCE=$TRIAL_CODE/acceptance/customer-trial-remediation-20260907
TRIAL_CREDENTIALS=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json
TRIAL_KUBECONFIG=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig
TRIAL_RELEASE=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/trial-customer-remediation-20260907
TRIAL_COHORT=r02
```

After root confirms the deployed source/image identities and authorizes observation, launch these as two separately tracked sessions (not an untracked shell background job):

```bash
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/observer/sample_cluster.py" \
  --credential-bundle "$TRIAL_CREDENTIALS" --kubeconfig "$TRIAL_KUBECONFIG" \
  --context k8s-inference-h100 --interval 25 \
  --output "$TRIAL_RELEASE/$TRIAL_COHORT/observer"
```

```bash
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/experience/interactive_sampler.py" \
  --credentials "$TRIAL_CREDENTIALS" --interval 25 \
  --output "$TRIAL_RELEASE/$TRIAL_COHORT/experience"
```

Capture baseline context and Qwen/HPA identity with the existing read-only helpers:

```bash
"$TRIAL_PYTHON" "$TRIAL_CODE/acceptance/customer-trial-20260907/observer/capture_context.py" \
  --credential-bundle "$TRIAL_CREDENTIALS" \
  --output "$TRIAL_RELEASE/$TRIAL_COHORT/observer/baseline-context.json"
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/observer/capture_qwen_state.py" \
  --kubeconfig "$TRIAL_KUBECONFIG" --context k8s-inference-h100 \
  --output "$TRIAL_RELEASE/$TRIAL_COHORT/observer/baseline-qwen-state.json"
```

Record source SHA, live CP/admin image digests, process/session IDs and exact observation start. Baseline should show Qwen's current hot Pod Ready, admin APIs available and no unexpected baseline workload blockage. Send READY to root only after actual sampler output and the first ordinary request; root then starts the scientific lane.

## During the cohort

When the workload lane sends the exact scientific START timestamp, update the private `experience/phase.json` via `apply_patch` with `phase=during`, the run ID and timestamp. The exporter uses actual timestamps, preserving rather than rewriting any raw phase-label lag.

Retain only meaningful milestones/incidents while the samplers run:

- Second BindCraft's frozen eligible pools, Kueue admission wait, actual node fit and successful resource release.
- Protenix's exact operation/attempt IDs, actual restore marker, container start boundary, terminal lifecycle ledger and admin displayed duration. Pair boundaries from the same attempt; do not infer restore use from a catalog option or invent `observed_at` timestamps.
- Actual node scale-up, image pulling/loading phases and absolute disk headroom per node, excluding old failed Pods from new failures.
- RF bulk shard preemptor identities, priorities, released attempts and automatic retries. Kueue priority preemption is not a cloud spot-interruption test.
- Every Qwen HTTP/MCP failure, including structured `isError`, request correlation and nested SDK errors. No hidden retry, cancellation or resubmission. If a real failure occurs, inform root promptly and preserve it.
- Qwen hot readiness and any natural burst lifecycle. An unobserved burst remains a coverage limitation; no new stress or policy changes to force one.

The browser/admin lane separately verifies downloads, policy controls and the repaired restore-duration card. Root owns any live intervention; expected capacity queues are not automatically defects.

## Recovery and stopping

After all 14 operations are terminal, retain the exact scientific END timestamp and update `phase.json` to `after`. Continue at least 90 seconds with at least four scheduled observer cycles and ordinary requests. Confirm all task-owned scientific resources released, no task-owned nonterminal Pods/Jobs, serving health returned, policies unchanged and no new unexplained restarts/pressure.

On root's completion signal, stop the ordinary sampler by creating its own private `experience/stop.json` with `apply_patch`. Wait for `sampler-completed.json` and clean session exit. Then retain a final cluster sample and stop only the verified observer PID using SIGTERM; wait for its completion receipt and clean exit. Never reuse r01 PIDs or send a signal to a guessed process. Preserve final context/Qwen state with fresh filenames. Root's deployment processes and existing live services are outside this cleanup.

If the manager session becomes unavailable after the completed campaign and bounded recovery, send the handoff and stop only these task-owned test processes to avoid unbounded traffic. Do not launch another cohort, change configuration or claim final acceptance without root.

## Whole-window publication evidence

The 25-second point samples missed a five-second r01 route withdrawal. Therefore, after each cohort, query the entire observation window—not just failure timestamps. Allow at least 30 seconds after observation end for normal log ingestion, without creating more inference traffic.

Set `TRIAL_OBS_START` to the earliest sampler start and `TRIAL_OBS_END` to the latest final sample/ordinary response timestamp. These must be actual ISO8601 UTC values from the retained receipts. Then run:

```bash
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/observer/capture_publication_window.py" \
  --credential-bundle "$TRIAL_CREDENTIALS" \
  --start "$TRIAL_OBS_START" --end "$TRIAL_OBS_END" \
  --output "$TRIAL_RELEASE/$TRIAL_COHORT/observer/publication-window" \
  --report "$TRIAL_ACCEPTANCE/observer/$TRIAL_COHORT-publication-window.json"
```

This reuses the private Loki helper, queries exact Qwen publication changes across all CP replicas in contiguous five-minute chunks, retains raw receipts/checksums privately, and exports only allowlisted fields. At most four hours/48 chunks; each existing query limit is 5,000 lines. Boundaries are deduplicated per event/Pod identity. Any failed/truncated query, malformed matching event or missing chunk makes evidence incomplete and exits nonzero. Narrow/supplement the incomplete range read-only; never call it clean because zero events were exported.

Every `withdraw` is retained and matched to that same CP Pod's next publication; unmatched withdrawals have unknown duration, not zero. A recorded withdrawal blocks clean route acceptance even when all sampled clients passed. `no-withdrawal-recorded` is deliberately narrower than an uptime claim: it must be considered alongside hot readiness, client results and ingestion/source limitations.

## Final exports and acceptance gate

Export only after both samplers have stopped, using the actual scientific start/end values:

```bash
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/observer/export_observation.py" \
  --raw "$TRIAL_RELEASE/$TRIAL_COHORT/observer" \
  --campaign-dir "$TRIAL_RELEASE/workload/$TRIAL_COHORT" \
  --output "$TRIAL_ACCEPTANCE/observer/$TRIAL_COHORT-observation.json"
"$TRIAL_PYTHON" "$TRIAL_ACCEPTANCE/experience/export_experience.py" \
  --raw "$TRIAL_RELEASE/$TRIAL_COHORT/experience" \
  --scientific-start "$TRIAL_SCIENCE_START" --scientific-end "$TRIAL_SCIENCE_END" \
  --output "$TRIAL_ACCEPTANCE/experience/$TRIAL_COHORT-interactive.json"
```

Root's bounded clean-cohort decision requires all 14 unchanged scientific operations/results/downloads complete without manual recovery; normal HTTP/MCP successful; no recorded route withdrawal; accurate repaired admin restore duration and controls; released resources and reconciled attempt accounting; no unexplained recovery, permission or availability defect. Missing hardware/transition coverage is stated explicitly, not replaced with zeroes or an exaggerated claim.

Only after r02 is fully assessed does root authorize r03 on the same final deployed revision. Two clean consecutive complete cohorts are required. Root alone owns commits, deployment and final Slack; no secrets or raw signed URLs go into repository reports.
