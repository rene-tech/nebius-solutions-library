# Repairs following the failed r02 customer campaign

R02 remains failed: 12 customer passes, one submission returning HTTP 409 after
durable acceptance, and one RF batch stalled during priority requeue. Original
requests, runtime identities, observations, and failed outcomes are retained.
Work resumed on 2026-09-08. This document records the deployed repairs and
verification evidence; full customer acceptance still requires two clean reruns.

## RF requeue and Pod accounting

The production controller used the frozen core's reservation-boundary transform,
which cleared `pod_uids` but retained `pod_lifecycle`. Construction then failed
with `Pod lifecycle evidence must uniquely bind an observed Pod UID`, leaving
the public operation running instead of completing its automatic retry.

`PolicyAwareScientificBatchController` now detaches transient lifecycle and
pending diagnostics before the existing core decides whether to fence the
reservation. Unchanged reservations return the original complete observation.
At a real boundary it preserves only later lifecycle evidence for Pod UIDs
already durably bound to the old attempt. Replacement Pods cannot inherit its
reservation or GPU ledger. Durable historical identities/signals are not erased.
Existing foreground deletion, absence confirmation, fresh retry identity and
queue clock remain in force. The qualified core and model recipes are unchanged.

The new production-path regression first reproduced the exact original
exception. Ten parameterized cases now cover reservation loss, reassignment,
and Workload recreation, including observations arriving after replacement
containers succeeded and observations carrying pending diagnostics. They verify
the old Pod's terminal accounting tail, exclusion of replacement Pod evidence,
unchanged normal observations, confirmed old-resource release before retry,
fresh attempt identity and eventual successful completion without intervention.

At 2026-09-08 07:15 UTC: 68 controller/telemetry/lifecycle tests pass; focused
Ruff and strict typing pass; `refresh_scientific_recipes.py --check` confirms all
runtime recipes are current without refreshing qualification. Existing unrelated
pytest temporary-directory cleanup warnings are retained, not test failures.

## Integrated admission, admin and startup changes

Admission now reuses only an exact matching frozen admission after concurrent
materialization, preserving true conflicts. Six new tests include a real isolated
PostgreSQL/API race; 98 related regressions pass. See
[admission evidence](workload/ADMISSION-RACE-FIX-r20260908.md).

The admin page continues refreshing successful runs until validated result
publication, and shows Finalizing results in the interim. It stops afterwards
and does not endlessly poll failed/cancelled runs. The live model form exposes
the optional startup-retention budget. All 164 UI tests and TypeScript pass.
See [UI evidence](experience/PUBLICATION-REPAIR-20260908.md).

Serving autoscaling gains a bounded initialization hold for already-requested
replicas, anchored to Deployment creation or an actual desired-replica increase.
It does not increase limits or renew indefinitely on Pod replacement. Unset
startup settings preserve existing model revision digests and Pod templates.
The optional initial Terraform setting is `models.startup_timeout_overrides`;
the existing live model UI controls subsequent changes. Three focused Terraform
contract tests and workloads validation pass, including bounds and omission.

The first integrated backend run had 1,631 passes, four optional skips, 77
external-service deselections and one obsolete compatibility-test expectation:
its independently assembled legacy payload included the new optional null field.
That expectation now omits the field while retaining the hard-coded released
digest assertion. All 72 fast-start/render/startup tests then passed; a full
rerun overlapped the exact-source image builds and passed: 1,633 tests, four
optional skips and 77 external-service deselections, in 237.50 seconds. No runtime recipe was changed.
An independent bounded integration check passed all ten RF cases and the actual
Terraform startup-timeout plans, with no blocking finding.

## Release and live acceptance

Root alone builds the integrated exact committed source and deploys through the existing staged
Terraform workflow. The retained stalled RF operation must settle truthfully;
an expired or failed old attempt will not be rewritten as a passing customer run.
After the last deployed correction, repeat the unchanged 14-operation campaign
twice with ordinary HTTP/MCP traffic and actual browser observation. A separate
burst check must demonstrate Ready, actual restore and useful serving, not merely
an allocated node or a Pod that is cancelled during image pulling.

Release source `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`, tree
`1beebf9a6c79fe8f8db43aab91e7a66a388833fe`, is pushed to main. Regional images:
control-plane `sha256:762510cb5354dea8f9d32834259dd164d561137582f4dd04aca18af4263b57bc`;
admin `sha256:fc7b0f2f8207eebc28576f55e9815de92a5c04bfb809beafb5c78ac5fae8b59c`.
Terraform uses the full exact repository archive, not a component-only archive.
The plan has zero infrastructure changes, the three expected foundation contract
refreshes, and 25 workload changes. All queue/flavor/cohort/priority manifests'
non-metadata content is unchanged. The CRD changes only its optional startup
property. Helm value changes are the two image pins, matching admin provenance,
and new immutable contract ConfigMap references. Bootstrap specs themselves are
unchanged; their execution identity/runtime image changes with the release.
Workloads apply completed. The control-plane, admin and model-controller
rollouts succeeded. Post-apply plans at 07:36 UTC show zero managed actions in
all three stages. The pre-apply plans and exact applied workloads plan are
retained privately; the post-apply plan SHA-256 digests are:

- Infrastructure: `41e5bdf31f9f954fb8d8925f39e2887b4ff604567251eaf49395814d88b74309`
- Foundation: `faec9439ab6783e13dca64775d39a2441f0696d46e92958faca31a0b69d754f0`
- Workloads: `1953c80c0f4eca2e0bea10693b563cbb0a96fd84d8c812abc6d6233a1a037ae4`

The retained RF batch recovered automatically, completed at 07:34:46 UTC, and
published a semantically validated result. All eight attempts released their
resources; two observations confirmed zero matching Jobs and Pods. No new
submission, cancellation or manual recovery was used. This separate recovery
does not change the original r02 failure. See [recovery receipts](workload/RF-RECOVERY-r20260908.md).

The deployed real-browser preflight at 07:35 UTC displayed the retained
Protenix phase duration as 3.95 seconds (API: 3.946846), reconciled occupied GPU
time, and downloaded an artifact with a recorded digest. The real admin form
then changed cold Cosmos startup retention to 1800 seconds and restored the exact
original omitted setting. The restored revision was observed Cold, with zero
replicas and no Pod activation throughout the scoped watch. Two earlier
harness-only failures stopped before mutation and remain retained in the
[preflight evidence](experience/admin-preflight-20260908.json).
Dedicated burst and two full customer-cohort acceptance checks remain in progress.
