# AMBER and NAMD: two completed hosted cohorts on Helm 226

Both engines passed two ordinary-key, exact-image customer workflows and
independent validation of their downloaded artifacts. This is a bounded
canonical-workload acceptance receipt, not a blanket scientific convergence,
GPU-snapshot, browser-UX or whole-platform readiness claim. The machine-readable
[AMBER receipt](amber.json) and [NAMD receipt](namd.json) retain complete digests,
operation IDs, source identities, evidence paths and explicit limitations.

All four operations used backend `bbab6ea4...` / Helm 226 and client
`aac7719c...`, without backend or engine-image mutations during these runs.
Each used one H100 80 GB HBM3, driver 580.173.02, pool `h100-ondemand-1x`.
The final read-only operation selector found zero remaining Pods or Jobs.
No quota change, eviction or additional infrastructure was used.

| Engine / repeat | Operation | Verified artifacts | Production native ns/day |
|---|---|---:|---:|
| AMBER 1 | `80fb3f64-d199-4884-b8ec-d503d38c69d7` | 39 | 733.84 |
| AMBER 2 | `b6ad10b6-8c2e-4002-8b1d-76201ad25715` | 39 | 736.24 |
| NAMD PME64 1 | `b38ab578-138f-4e5d-b708-18e33fa95bf5` | 77 | 412.245 |
| NAMD PME64 2 | `5d3ae566-0a2a-4a4d-9917-acea617d4d30` | 77 | 412.385 |

AMBER values above are its printed production timing. NAMD values are the final
native `PERFORMANCE ... averaging` line. NAMD's full process-wall values are
411.2286 and 411.4312 ns/day; the validator's median of the final ten native
`TIMING` records yields 412.8855 and 413.3304 ns/day. These measure different
intervals. Hosted acceptance-to-completion includes scheduling, checkpoints and
artifact publication and is not native production throughput. AMBER queue
duration was not independently captured and is not inferred from API timing.

## Scientific and identity gates

The immutable master has 6,598 atoms: ff14SB ACE–ALA–NME plus 2,192 TIP3P waters,
with 15 Å construction padding. Each workflow completes minimization, 100 ps NVT,
100 ps NPT and 1 ns NPT production at 2 fs, hydrogen-bond constraints, 300 K,
1 bar, 10 Å unswitched nonbonded cutoff, dispersion tail, explicit 64³ PME grid
and order 4. Native per-engine thermostat/barostat algorithms remain disclosed;
identical force-field inputs do not make different integrators identical.

- AMBER: native SCR/LFMiddle with explicit `skinnb=2.0`, `skin_permit=0.5`.
  Every native PME grid/order echo was checked. The separate immutable-input
  audit binds all 12 input members, the normalized recipe and all 38 output
  files. Exact worker v3, run CPU-only for validation, checked finite matching
  coordinate/velocity coverage of 100/100/1,000 frames, the canonical master,
  1,000 genuine molecular-virial pressure observations and CPPTRAJ coverage of
  all production frames. All six requested native commands completed.
- NAMD: all four native stage logs must explicitly report 64³ / order 4 and the
  requested tolerance. The same scientific validator audits immutable inputs,
  exact recipe/inventory, duration, seeds, restart step and 100/100/1,000 finite
  DCD frames. Native group pressure is the barostat observable; atomic pressure
  is retained separately. All four requested native commands completed.
- The shared offline identity audit additionally checks original request bytes,
  operation ownership identity, pinned engine/schema, exact ordered completion,
  zero native command exits, full output inventory, every original archive
  member and released compute. It does not replace either scientific validator.

The fixed seeds are intentional operational/scientific repeats, not independent
statistical replicas. AMBER's printed mean 298.32824 K uses the native staggered
LFMiddle kinetic estimator; archived full-step velocities use a different
estimator. See the source-bound [temperature note](../../amber/qualification/TEMPERATURE_ESTIMATORS.md).
Do not silently relabel either temperature or infer convergence from a 1 ns run.

## Preserved variants and evidence limitations

The previous NAMD `PMEGridSpacing 1.0` inputs selected native 44³ dynamics grids.
Operation `50fe9bf7-0f72-4639-b33e-45ca3c364e0e` and its original passing
then-current validation remain retained as that variant. It is not final
common-grid evidence. Commit `cf91f429...` freezes a **new** archive and adds
native-grid semantic gates; only the four dynamics-script PME directives differ.
The original `delivery-01` was not edited in place. Final NAMD input SHA256 is
`79d2ef59dd92512873b5f36a7fc36598ca921ec8dae3af5f24759673df6064fe`.

AMBER cohort 1 lacks a separate retained per-Pod image capture. Its exact image
is bound to the coordinator-confirmed unchanged Helm 226 activation, scheduling
snapshot and pinned native engine/recipe. Cohort 2 has a Pending Pod image
specification plus the later actual GPU query; this is not misrepresented as a
running image-ID capture. Both NAMD cohorts have running Pod image-ID captures.
The current API/controller image and readiness were independently observed.

Historical upstream SPFP sodium-TI numerical failures, default AMBER neighbor
margin failure, earlier collector-registration failure, storage-quota failure
and failed snapshot screens remain separate evidence. This cohort neither
deletes nor reclassifies them. Native L40S and restart qualification is separate;
these hosted repeats qualify only the named H100 workload.

## Reproduction and tests

Use the exact frozen inputs and released client with an ordinary authorized key.
Keep source input archives, submitted requests, original client receipts and
native output files immutable. `materialize_customer_results.py` verifies every
download and rebuilds native workspaces; then run the existing engine validator
and [identity audit](../audit_hosted_identity.py). AMBER validation uses
`/usr/bin/python3` in exact worker v3, CPU-only, `--network none`; it already
contains NumPy, netCDF4 and the pinned runtime package.

The NAMD fixture/validator suite and new identity-audit negative tests pass
**80 tests**. Cases reject the old grid, wrong PME order/tolerance, changed
recipe/engine/operation, incomplete stages, unreleased resources, corrupted
outputs, rehashed altered scientific input and ambiguous archives. Ruff and
`git diff --check` pass. Unrelated old PostgreSQL-socket pytest cleanup warnings
were not treated as test failures or grounds to delete unrelated files.

Public receipts intentionally omit keys, caller fingerprints, signed object
URLs and licensed source. Raw customer evidence remains in task-owned private
directories; published digests make its identity independently checkable.

## Final additive client: completed-result recovery

Exact client `d074317715d911addafc4b27860eb8088e0ebce6bdd21fde1f4afacda22a1c03`
subsequently recovered all four completed operations with the same ordinary key
into **fresh empty directories**. No original receipt or artifact was copied.
The client receives only `--recover-operation-id` and `--output`; it performs
read-only status/result RPCs and artifact-content GETs, never admission, upload
or cancellation. This is **artifact recovery, not a new simulation**.

[The final-client recovery receipt](final-client-recovery.json) records 232
artifacts / 603,925,652 bytes plus four manifests. Every content download has
`verified-copy` and one transfer attempt: 236 fresh content GETs supported by the
receipts and inspected transport, not packet-capture telemetry. All four
manifests are byte-identical to the original scientifically validated outputs;
every downloaded file was independently size/SHA-256 checked again.
No Pods or Jobs for these operations existed after readback.

Direct network-disabled inspection of both exact images proved byte identity of
`invoke-scientific-batch.py` (`7e5c7b36...`) and `scientific_receipts.py`
(`d0064416...`). The new image adds an isolated analysis environment from source
`c9a1481f...`; its browser/analysis-environment acceptance belongs to the
workbench owner, not this readback receipt. The original simulation client
remains recorded as `aac7719c...`; it is never retroactively relabeled.

The reusable [recovery helper](../recover_image_customer_results.py) checks the
original owner/endpoint identity before starting, mounts no input bundle,
retains complete client output in a sibling mode-0600 log, and emits a compact
sanitized summary. Its output directory must not already exist:

```bash
python3 k8s-inference/models/molecular-dynamics/qualification/recover_image_customer_results.py \
  --image "$EXACT_CLIENT_IMAGE" \
  --operation-id "$COMPLETED_OPERATION_ID" \
  --key-file "$OWNER_KEY_FILE" \
  --previous-receipt "$ORIGINAL_VERIFIED_RECEIPT" \
  --output "$NEW_RECOVERY_DIRECTORY" \
  --mcp-url https://89.169.99.188/mcp
```

The recovery helper adds 14 passing tests; the combined NAMD, immutable-identity
and recovery suite passes 94 tests. This same-owner readback does not replace
cross-tenant denial or full browser-agent acceptance.
