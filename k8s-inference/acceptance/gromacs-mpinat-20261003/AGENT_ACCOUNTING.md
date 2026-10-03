# Actual-agent evidence join

This is an acceptance adapter, not a second accounting system. `agent_ledger.py`
projects selected-case-verified native results into the existing `Ledger.cohort`
format with interface `agent-skill-MCP`. `ledger.py` and `cost_report.py` remain
the owners of work, phase, allocation and efficiency calculations. Raw REST,
raw MCP and actual-agent execution remain separate cohorts.

The one final augmentation is complete at **18:05:31.665361 UTC, 3 October
2026**: [actual-agent benchmark delta](AGENT_BENCHMARK_DELTA.md). It adds the 18
R5 native completions, six original completions, fourteen zero-additional-GPU
recovery/report associations and preserved historical failures. The preparation
and first-pair sections below remain historical evidence; they were not rerun
or rewritten as the final snapshot.

The first-pair check below is preparation for **one** final bounded snapshot
after the remaining R5 native work is terminal. It is not a claim that all agent
cases passed. Current release/delivery qualification remains in
[AGENT_QUALIFICATION.md](AGENT_QUALIFICATION.md); actual Lynx feedback versus
representative assumptions remains in [LYNX_WORKLOADS.md](LYNX_WORKLOADS.md).

## Evidence boundary

Each explicit selection binds actual chat tool evidence → native operation →
exact parameters/input hashes → native recipe/result/logs → verified report.
The saved-study path additionally binds chat admission and the immutable study
plan. The supported direct batch-CLI/MCP path binds the actual verified-delivery
tool call and report in the final reply; it never invents a saved study. The
adapter reuses `validate_plan`, `direct_delivery`, `validate_recovery_identity`
and `verify_timing_report` as appropriate.
It rejects failure proofs, admission-only/nonterminal studies, unrelated
operations, changed recipes/parameters, hash drift and duplicate native charges.
It does not search a live campaign and treat whichever outputs exist as passes.

A saved study may use the chat's output directory or a validated descendant
(for example `case/final`). Exact frozen study-plan/native/report bindings still
apply; siblings, non-workspace paths and traversal are rejected. The adapter
retains chat and study output paths separately. The final collection exposed
and corrected an equality-only metadata check for real ligand-cmet-ti; that
failed local projection is collector history, not a native/model failure.

Only metadata, native result JSON and logs are copied. Large trajectory, TPR,
checkpoint and energy payloads remain original SDK references; each projected
artifact explicitly says `rehashed-local` or `original-SDK-reference-only`.
Full original-input verification belongs to the retained selected-case proof;
this adapter does not falsely claim it rehashed an uncopied TPR. Original SDK
artifact counts/bytes remain assertions with their existing cost-report label.

`agent-metadata.json`/`agent-join.json` attach client digest, seeded model,
reasoning setting, instructions hashes, conversation/study IDs, tool/error
metadata and authenticated delivery proof. Their timing scopes are separate:

| Field | Meaning |
| --- | --- |
| Chat elapsed | Actual response including tools; saved-study admissions and direct terminal deliveries have different scopes |
| Tool seconds | Harness sum of tool-call durations; not necessarily disjoint wall intervals |
| Saved-study elapsed | `finished_at − created_at`; includes staging, native execution, polling and analysis; null for direct-MCP delivery |
| Planning/wait/delivery seconds | Null: no independent phase timers; do not infer by subtraction |
| Native wall/counters | Existing wrapper-command and GROMACS counter scopes; not pure GPU integration |
| Allocation clocks | PostgreSQL lifecycle rollups and independent observer bounds; never chat wall |
| Agent token cost | Null where provider usage/rate evidence is absent |

The ordinary native phase unknowns remain null: pure integration, native
initialization, checkpoint and export durations. `active_compute` is the broad
application-observed lifecycle phase, not a new pure-compute measurement.

### Direct delivery, recovery and customer-path errors

Selected proofs may declare `agent_path="direct-batch-mcp"`, with
`study_id`, `model_step` and `plan_identity` all null and
`direct_delivery_verified=true`. Such selections omit `frozen_record`; they do
not require a `durable-terminal-state.json`. The original native request/status,
authenticated parameter/provenance files, native recipes and delivered report
still have to match. Actual report paths and final-reply delivery are rechecked.

A direct recovery uses two **real, separate** receipts. Its `recovery_binding`
contains `submission_receipt_file`, `submission_receipt_sha256`,
`recovery_receipt_file`, `operation_id` and `same_operation_verified=true`.
The adapter rechecks the original receipt hash and same operation/caller/endpoint,
reads request identity from that original receipt, and uses the recovered
terminal status/artifacts. It neither rewrites a running observational receipt
as completed nor merges original identity into a synthetic recovery receipt.
The existing ledger sees one operation/attempt set, not a recovery GPU replay.

`customer_outcome` retains `observed_tool_errors`, `delivery_outcome`, the
proof's `customer_path_clean` and the conservative combined clean-state value.
Any observed tool/chat failure or transport warning keeps the path non-clean,
even if native execution/report verification passed. An older proof lacking a
clean flag remains unknown, not clean by default. `transport-warnings.json` is
frozen alongside summary warnings when present. Put failed supervisor batch
receipts in the selection's `history` array so they remain separately hashed
and retained; a later native proof does not revise a failed batch verdict.

Concrete R5 compatibility cases, reviewed without collecting another snapshot:

- CMET-TI `cb68970e…`: direct batch/MCP delivery, no saved study. Its earlier
  proof lacks a clean-path flag, so that value remains unknown.
- SHP2-TI `b7d84d7b…`, chat `466f822a…`: same-operation recovery delivered the
  report after exit **75**, then recovery-command exits **2** and **1**.
  `customer_path_clean=false`, `delivery_outcome=verified_after_tool_errors`;
  retain `agent-remaining-r5/batch-07.json` as failed customer-path history.
- benchBTI chat `9e45cbf0…`: retained GET-status **503 SERVER_NOT_READY**, retry
  after one second, recovered through GET-only retry. No native resubmission;
  successful native proof does not make that path warning-free.

These are interface/outcome distinctions, not new throughput/cost measurements.
The first-pair snapshot and all prior reports remain unchanged.

## Reusable collection command

Use a NEW private output path. Select exact proof files; `*-failure.json` files
may be retained in `history`, never used as successful proofs. Example manifest:

```json
{
  "cases": [{
    "proof": "/private/selected-case-verification/case.json",
    "chat_directory": "/private/replay/model/mpinat-benchsfc",
    "frozen_record": "/workspace/.scientific-studies/OWNER/STUDY/receipt.json",
    "history": ["/private/previous-observation-failure.json"]
  }]
}
```

Run from `k8s-inference` using its existing control-plane environment:

```sh
FS2_PY=components/control-plane/.venv/bin/python
FS2_CODE=acceptance/gromacs-mpinat-20261003
"$FS2_PY" "$FS2_CODE/agent_ledger.py" \
  --manifest /private/explicit-selection.json \
  --workspace /private/exact-isolated-client/workspace \
  --campaign-plan /private/replay/plan.json \
  --output /private/NEW-agent-snapshot
```

`--sudo-read` is only for the already-authorized, root-owned isolated QA mount;
it runs bounded local `readlink`/`head` reads, never a workspace permission change.
Sources remain read-only. The fresh directory requirement prevents accidental
overwrite of fixed snapshots. The helper performs no upload, submission,
cancellation, credential/policy or customer write.

On that NEW output only, the existing collectors provide the rest:

```sh
"$FS2_PY" "$FS2_CODE/capture_plans.py" --campaign /private/NEW-agent-snapshot
"$FS2_PY" "$FS2_CODE/recover_bucket.py" \
  --qa-env /private/existing-system-qa.env \
  --cohort /private/NEW-agent-snapshot/cohort-1 \
  --output /private/NEW-agent-snapshot/recovered/cohort-1
"$FS2_PY" "$FS2_CODE/ledger.py" \
  --cohort /private/NEW-agent-snapshot/cohort-1 \
  --recovered /private/NEW-agent-snapshot/recovered/cohort-1 \
  --telemetry /private/NEW-agent-snapshot/frozen-telemetry \
  --database /private/NEW-agent-snapshot/measurements.sqlite \
  --interface agent-skill-MCP
"$FS2_PY" "$FS2_CODE/cost_report.py" \
  --database /private/NEW-agent-snapshot/measurements.sqlite \
  --cohort /private/NEW-agent-snapshot/cohort-1 \
  --output /private/NEW-agent-snapshot/report.json
```

Freeze complete telemetry records at one recorded cutoff before indexing;
retain original observer byte-prefix and selected-copy hashes. Collect only
the selected operation/attempt IDs and their Pod UIDs, plus node shape metadata.
Do not replace gaps with zeroes or repeatedly rewrite the old R1/R2 reports.

For durable allocation clocks, reuse the bounded projection in private
`agent-accounting-pair-r5-plan/build_pair.py` (also used by the earlier delta
sidecar): `fs2_telemetry_subjects LEFT JOIN fs2_reporting_lifecycle_latest
USING(subject_id)`, explicit UUID set, `tenant_id='system'`,
`principal_id='qa'`, `workload_kind='scientific_batch'`, and the exact model.
Use the existing `capture_plans.py` CNPG discovery/read-only `PGOPTIONS` pattern;
retain query/hash, capture time, rollup timestamps, attempt IDs and data gaps.
No full state or database credentials are exported. Link the additive sidecar
to the immutable report SHA. Use the **same** dated per-allocation price from
`cost_report.py`, not another price model.

## First-pair result — fixed preparation snapshot

Private snapshot: `agent-accounting-pair-r5-r1`, under the task's secure handoff.
Cutoff **2026-10-03 15:15:49.679670 UTC**. Two original operations, two attempts,
six mdrun repeats, 20 commands total, 20 checkpoint manifests, 98 measurements,
107 scoped observer records and two GPU allocations. Checkpoint recovery found
40 verified text files and zero detected native errors. No trajectories copied.

Both used one L40S from the observed AMD 4-GPU VM, single-GPU native image
`dc5d908c…`, R5 client `b948ecca…`, `moonshotai/Kimi-K3`, high reasoning, and three
10,000-step repeats. Each delivered 0.06 ns total; these are repeated timings of
the same starting input, not three independent scientific ensemble samples.

| Case / operation | Native median ns/day | Durable particle×steps | PostgreSQL occupied GPU-s | Allocation-share USD |
| --- | ---: | ---: | ---: | ---: |
| SFC / `c966e97d…` | 380.962 | 100,890,000 | 71 | 0.0450534 |
| ligand-cmet-eq / `7c73ac3f…` | 246.305 | 193,290,000 | 76 | 0.0482262 |

The two production occupancy clocks fall inside offline bounds 71–84.812023
and 72–86.794863 GPU-s respectively. Both rollups are reconciled,
`application_observed`, with `trace_context_missing`; ligand also retains
`phase_classification_incomplete`. Costs use the retained **2026-10-03** public
on-demand allocation-share scenario, $2.2844/GPU-hour for that observed VM.
They are not invoices, GPU utilization, agent token costs or full-node charges.
No public matched-speedup claim is made.

Both initial chats took about 24.13 seconds and truthfully reported admission.
Their native-report completion was proved later by durable saved-study files
and authenticated downloads, not inferred from that first response.

Immutable evidence SHA-256:

- `report.json`: `6c7a50cae86ffe04cf64cf1d0a1494e9e6faebf5faa1f9035ab573d78f004f7f`
- `agent-join.json`: `adf4b01308a3fd222f83bc5f59d61530acca5a55b2490a8edca9b4c507812e3c`
- `durable-lifecycle-sidecar.json`: `d21eabe86b0a9b7628882ee4a9f4e6b7d8e02346ea84e39ee19d85ed9d3f5a85`

## Final single-snapshot gate and recovery history

Do not run the full augmentation until all 18 remaining R5 native operations
are terminal and each selected case has either a verified delivery or an
explicit retained failure. Missing proof is unknown, never an implicit pass.
Freeze that exact operation set/cutoff once. Index original failures and retries
using existing receipt/status/checkpoint evidence; their absence from successful
proofs must not remove them from the ledger or retry-waste discussion.

The six prior native-complete cases are **not** rerun or renamed as R5 native
execution. Retain the original runtime and operation IDs:

- SFI `fb0c2f48-fe9c-47d1-97b5-c8efc79992fb`
- SNC `2f4fdcb5-aae3-4b7e-acf9-07bdc8196a9a`
- SNI `c71db1d0-ff59-496e-9b78-4bc4ade9f0a1`
- STC `925bed89-2914-4977-9535-a1295f4ea4cc`
- STI `292d2dd1-68aa-4084-afdc-74f96b2dec03`
- MEM `7c59f87a-d62c-46aa-bd1c-14cb1a5cf0e6`

Attach later analysis/report conversations as separate associations to these
same IDs. `unique_operations` deduplicates that native identity and rejects
changed input/recipe identity; a newer analysis client never changes the native
image or creates another GPU charge. Retain old empty-report, tool-budget,
wrong-upload and observation-timeout evidence. An observation timeout is not
automatically a failed native run; the R4 wrong metadata upload is not MD work.
An R5 advisory/client failure likewise remains agent-side evidence unless a
separate native operation proves MD failed; it is not another GPU attempt.
`reconcile_agent.py`, `verify_agent_recovery.py` and the selected-case verifier
remain authoritative for those historical distinctions. A final projection of
their original operation records still needs its own explicit selection; this
first-pair adapter does not relabel historical recovery proofs as R5 admissions.

Clone/reference the fixed R2 native ledger once into a NEW final snapshot, then
add each distinct agent-native operation once with this existing ledger format.
Keep REST/raw-MCP baseline, MPI runtime/PME variants, actual-agent original
native results and client-only recoveries distinct. Do not merge intentional
timing repeats or different protocols as retries. Link a concise new delta to
the original [BENCHMARK_DELTA.md](BENCHMARK_DELTA.md), not a rewritten history.

Tests:

```sh
components/control-plane/.venv/bin/python -m pytest -q --tb=short \
  acceptance/gromacs-mpinat-20261003/test_agent_ledger.py \
  acceptance/gromacs-mpinat-20261003/test_verify_agent_case.py \
  acceptance/gromacs-mpinat-20261003/test_verify_agent_study.py \
  acceptance/gromacs-mpinat-20261003/test_ledger.py \
  acceptance/gromacs-mpinat-20261003/test_cost_report.py
```

Result: **65 passed**; Ruff passed for the two new Python files.

After the direct/recovery compatibility extension: **70 passed** using the same
focused command; scoped Ruff passes. New fixtures cover direct delivery without
a study, original→recovery binding/caller/hash drift, one native ledger charge,
null study timers, retained three tool failures, older unknown clean state and
saved-study transport warnings. No full-campaign snapshot was collected.
