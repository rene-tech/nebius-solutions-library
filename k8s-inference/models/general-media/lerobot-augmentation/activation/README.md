# Additive LeRobot activation

The canonical scientific profile, request schema and execution row are packaged
in the control-plane image. The CPU worker is separately pinned by its registry
manifest digest. `active` enables ordinary HTTP and typed MCP admission for keys
with both the LeRobot and Cosmos model grants; it does **not** mean qualified.
During onboarding the public-completion and scheduler-eligibility receipts stayed
null. The current qualified projection binds the corrected worker's actual
release150 [public completion](qualification/public-completion-r150.json) and
[scoped evidence index](qualification/evidence-index-r150.json), with its exact
scheduler receipt addressed by SHA-256 in `workload-profile.json`. The historical
release148 proof remains intact and does not qualify successor image identities.

The [final151 additive receipt](qualification/final-cohorts-r151.json) records
two unchanged ordinary-key lighting/environment HTTP/typed-MCP cohorts and a
two-variant transfer case: five positive parents, six datasets, 12 successful
generation children and 11 reused/sequential delegated uploads. Every generation
used one attempt; both in-flight and terminal idempotent replays passed. The
independent LeRobot 0.6.1 reader decoded 768 camera frames and compared 13,824
nonvideo values exactly. This report changes no packaged profile, runtime,
execution map, scheduler or deployment.

| Final151 case | Client end-to-end seconds | Server parent seconds | First child activation seconds |
| --- | ---: | ---: | ---: |
| Cohort1 lighting, MCP | 119.136 | 80.345 | 38.320 |
| Cohort1 environment, HTTP | 113.578 | 73.155 | 0.727 |
| Cohort2 lighting, HTTP | 117.755 | 79.364 | 37.998 |
| Cohort2 environment, MCP | 151.547 | 109.996 | 37.934 |
| Supplemental two-variant transfer, MCP | 155.522 | 95.369 | 37.917 |

Client time includes preparation, uploads, polling, downloads and independent
validation; it is not GPU time. The short warm activation fields are bookkeeping,
not model-load latency. Reported cold-start intervals do not prove new-node
provisioning. An actual `cuda-criu-restored` marker is retained for the first
supplemental transfer child. Only three of the 12 positive generation rows have
exact Pod/node/GPU identity; nine are unavailable, never measured-zero usage.

Malformed input returned 422; nonexistent episode selection returned
`INVALID_REQUEST` with zero children. Concurrent parent admission returned
429 `concurrency_exceeded`. Base cancellation reached CPU `active_compute`
and released resources before child admission. A separate operator-assisted
probe confirmed its native child running through the ordinary public key,
cancelled its parent at 02:10:30.162142 UTC, and observed the child cancelled
at 02:10:30.537031 and parent cancelled/released at 02:10:32.987380. No further
generation unit was admitted. This verifies lifecycle fencing, not immediate
GPU-kernel interruption or public child discovery. The earlier extra probe
missed its observation window; another hit an acceptance-only native DTO parsing
bug and cancelled after all generations finished. Both failures are preserved.

Read-only cleanup found zero active operations for the exact disposable
principal, all ten task CPU Jobs/Pods absent, and native Cosmos naturally at
zero desired/observed replicas with no Pods. No manual scale/delete was used.
Grants, canary revocation and final deployment verification belong to the release
owner's journal; this receipt does not include unrelated app-wide historical usage.

Scientific recipe qualification and the generic Admin **Last qualification**
badge are separate projections. This release does not mount the optional
customer-readiness verdict index, so that header still says **No recorded
qualification** even though the scientific proof and actual cohort receipts
exist. It is an unpopulated UI evidence projection, not evidence that these
tests did not run. The existing optional integration uses
`catalog.customerReadinessConfigMapName` / `customerReadinessKey` and gate output
from `acceptance/customer-readiness/capability_gate.py`; wiring a new mount is a
separate coordinated rollout, not part of this frozen acceptance release.

The first release147 public MCP dataset attempt is retained as a
[failed integration receipt](debug-failure-r147.json), not qualification. Its
attempt-scoped upload succeeded and generation was admitted under the parent's
concurrency-one policy, but the worker misread the bare operation response and
failed before polling. Parent fencing cancelled the activating child; no GPU
generation or output dataset completed. Corrected worker `df364675…` then passed
the real public dataset path, both idempotent replays, and the independent
LeRobot 0.6.1 reader: two episodes/two cameras, 128 decoded camera frames and 2,304
exact nonvideo values. Warm lighting appeared, but fruit appearance and fine
gripper details also changed. Strictly lighting-only edits and physical/action
fidelity are not qualified.

Release149 then passed four single-variant public HTTP/MCP dataset-reader cases,
but its supplemental two-variant blur run failed: the second variant attempted
to PUT bytes into the first variant's finalized input upload and received HTTP
409. The first generation succeeded; no second generation was admitted. The
[negative receipt](debug-failure-r149.json) preserves the actual worker log,
parent and child identities. Further admissions stopped before the negative and
cancellation probes. These partial cohorts do not complete final acceptance;
the corrected successor started active/unqualified and acquired its own150 proof
before the final151 cohorts above.
Independent environment review also found that requested laboratory background
replacement did not visibly succeed and robot trajectories changed. Successful
dataset mechanics must not be described as semantic-intent or action-fidelity
qualification.

The CPU parent requests 2 CPUs, 16 GiB RAM and 32 GiB ephemeral storage, with
4 CPU/24 GiB RAM/32 GiB storage limits. Its ordinary scientific companion and
existing general-CPU queue remain unchanged. The worker rejects source bundles
over 5 GiB, expanded datasets over 8 GiB, and requests whose conservative
workspace estimate cannot fit 32 GiB before contacting Cosmos. Each output is
bounded to 5 GiB. See the [worker limits](../runtime/README.md).
The coordinator is CPU-only, not a GPU-snapshot workload. The existing
`cosmos3-nano` App owns the delegated GPU runtime's snapshot, hot replicas and
autoscaling policy; those settings are independent of this CPU execution row.

Existing ten scientific profile receipts are retained unchanged. Their original
whole-map digest is represented by `qualification_baselines`: the ordered set of
old model IDs must reconstruct the **exact** historical map hash. Any changed or
removed image, stage, mount, resource, namespace, environment or identity fails
loading. A new model cannot borrow an older model's proof. Current run receipts
still bind the complete current execution-map recipe; full configuration
identity additionally includes the snapshot registry and baseline metadata.

The rollout owner captures current Helm values and the scheduler ConfigMap's
exact bytes, then renders an additive overlay:

```sh
python3 render_overlay.py \
  --baseline-values /private/baseline.values.yaml \
  --baseline-scheduling /private/baseline.scheduling.exact.json \
  --output-dir /private/lerobot-activation-candidate
```

The renderer refuses changed/deleted old model rows, preserves every live
snapshot bundle, checks the scheduler's captured byte hash, and adds only the
LeRobot eligibility row using the existing Cosmos eligible pools. It emits a
new content-addressed scheduler ConfigMap plus a `scientificBatch`-only Helm
overlay. Apply neither an old whole values file nor an older reduced model map:
the live speech, storage and serving catalog must remain intact. The owner
applies the new ConfigMap and rolls out the matching control-plane image.
For a reviewed successor, capture the current full baseline and add
`--replace-lerobot`: this permits only the existing LeRobot image and execution
identity to change. Resource, mount, placement or sibling-row changes still
fail. An unchanged scheduler ConfigMap is reused, not rewritten.

The Terraform facade uses the same strict baseline reconstruction in
`scientific-execution.tf`; it keeps the raw Helm map hash separate from the
current recipe hash. A scientific-only App also needs the per-cluster
`deployment.scheduling.model_eligible_pool_ids` entry. For this H100 deployment,
`cosmos3-lerobot-augmentation = ["h100-reserved-8x"]` preserves the existing
Cosmos eligibility declaration. This selects no GPU for the CPU parent; each
delegated Cosmos child retains the native model's own serving placement.

No local test or historical preview is a completed public LeRobot workflow.
After deployment, retain actual ordinary-key upload/submit/child operations,
downloaded bundles, full pinned-reader reopen/decoded frames, action/timestamp
comparisons, idempotence and cancellation evidence. Promote only with those
actual receipts, then complete the unchanged customer cohorts. Numeric-record
preservation is not proof of visual/action fidelity or downstream training
quality; transfer and video-to-video retain their documented limitations.
