# MindEval event operator runbook

## Deployment boundaries

The workshop is additive to Scientific AI. Do not replace the platform with a
stale branch: preserve recently deployed storage, speech, model catalog and
admin-console changes. Deploy immutable images and record source commits,
digests, prior release versions, project/region, GPU placement and verification.
The current event target is project `project-e00rene`, eu-north1, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`; never assume the current kubectl context.

Public entry points are `/workshop`, `/v1/workshop`, `/v1/mindeval` and the
platform speech/MindGuard endpoints on the configured origin. No port forwarding
is required for attendees. Existing `/admin`, `/mcp` and scientific endpoints
must still work after a rollout.

Deploy the shared control plane first when authorization/voice contracts change,
then the additive workshop chart. Managed voice Apps use the existing controller
and ordinary model permissions. MindGuard's optional Helm deployment is
documented in `charts/addons/mindeval-workshop/MINDGUARD.md`.

The provider gateway has one persistent scheduler. Do not increase its replica
count to increase throughput. Scale workshop workers/API replicas only after
accounting for database connections, the global fifty-call scheduler and actual
provider rate limits. Do not increase provider/cloud quotas as an implicit fix.

## Before admitting attendees

- Freeze gateway, workshop, control-plane and model image digests. Record the
  deployed chart values without secrets and the provider catalog snapshot.
- Issue ordinary platform credentials for ten team principals in the intended
  event tenant. Grant `inference.invoke`, the `mindeval` workflow and the speech /
  MindGuard models used, or the explicitly approved full catalog. Set concurrency
  to five and expiry beyond the actual event. September diagnostic keys expire
  after one day and **are not October attendee credentials**.
- Confirm the selected public clinicians, patient and fixed non-conflicting judge
  are still offered by Token Factory. Never substitute a missing model silently.
- Confirm speech Apps and classifier replicas are Ready on healthy GPUs, with
  the required hot floor. Record cache/snapshot modes as actually deployed;
  do not describe a weight cache as GPU snapshot restore.
- Complete two unchanged-release rehearsals with ten teams, full-dialogue
  coverage of each selected clinician, strict judge parsing and complete
  classifier prefix coverage. Retain API payloads, reports, timing and errors.
- Verify spoken playback, typed/microphone intervention on either role,
  reconnect, abort and resume from the public participant path.
- Verify provider transient-error handling and scoped speech/pod interruption.
  A graceful Pod drain is **not** evidence of abrupt VM or physical-node loss;
  label each failure experiment accurately.
- Supply the attendee guide and a clearly labeled **prerecorded demonstration**
  fallback using synthetic profile recordings. Never replay a recording while
  presenting it as a live response.

Private MindGuard v2 is a separate gated clinician artifact. Its absence must
not be hidden by renaming the public classifier. The public test dataset and
model-weight access are also separate approvals; successful model loading does
not establish clinical accuracy or agreement with unavailable gold labels.

## Monitor and recover

Use workshop run events/reports and gateway `/v1/mindeval/queue` to distinguish
queue wait, provider inference, speech generation/transcription and errors.
Use ordinary platform model/usage views for speech calls and Prometheus metrics
for the services. Report provider token usage separately from GPU model usage;
MindGuard currently reports its observer metering limitation explicitly.

A healthy queue drains fairly. If a provider becomes unavailable, preserve the
original failed/interrupted attempt. Select an available clinician for a **new,
labeled** comparison instead of mutating an existing run's model. The offline
recording is the fallback when live inference is unavailable.

If a workshop worker disappears, its lease expires and unfinished work becomes
interrupted. The operator/attendee explicitly resumes; do not blindly retry all
requests. Provider work may have been billed before a connection was lost.
Pause/takeover changes the run version so superseded outputs cannot overwrite
human input. Database contents and the encryption-key Secret are a recovery
pair; do not rotate/delete the key while active runs still need it.

For a bad deployment, identify the exact previous digest and preserve current
schema/data. Roll back only the task-owned services or reviewed shared release.
Do not delete PVCs, tenant data, model caches, shared node groups or pending jobs
as a shortcut. Record any temporary test capacity and restore intended minima.

## Existing-cluster caveats discovered during rollout

On 2026-09-16, two preemptible H100 nodes were already unreachable before this
workshop rollout. Their GPU-observer DaemonSet pods prevented Helm's release-wide
wait from succeeding even while all new control-plane/controller replicas were
Ready. Upgrade 133 timed out and rollback 134 also timed out on the same baseline.
The subsequent shared release used hook completion plus explicit readiness of
the changed Deployments; the unavailable nodes were not marked healthy, force
deleted or hidden by loosening disruption limits. Capture current node state
again before the event; this is not a permanent exception to acceptance.

An existing platform maintenance Job also failed deleting an expired operation
referenced by `fs2_scientific_stage_attempts`. This predates the workshop image;
the operation/evidence remains intact. Track this retention-cleanup issue
separately rather than deleting scientific evidence or changing its foreign keys
inside the workshop rollout.

## Acceptance evidence, not a blanket readiness claim

The first public diagnostic completed 60/60 two-round jobs and a separate
ten-round dialogue, but classifier requests failed an internal Host check. Those
reports are intentionally retained as diagnostic evidence. The Host fix and
strict classifier gate require fresh final rehearsals; the diagnostic is not
relabelled as an event-ready pass. Consult the release evidence and Task Deck
acceptance card for the latest actual result and any remaining blockers.
