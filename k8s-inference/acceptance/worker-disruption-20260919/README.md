# Retain worker disruption after Pod removal

Release189's exact owned RFdiffusion attempt was evicted through Kubernetes at
17:01:34 UTC on19September2026. Operation
`c767ebaa-e39f-4874-a833-60a1b910238d` then failed after one attempt with
`BackoffLimitExceeded`, classified as an application failure. The saved policy
already allowed two stage attempts; no limit change is needed. Earlier ESMFold
recovery on release183 does not qualify this distinct failed RFdiffusion case.

The pre-eviction receipt showed both containers running. The Pod was gone by
readback; its terminal reason was not retained. A missed-Pod-observation race is
a diagnosis consistent with this evidence, not a recovered terminal Pod fact.
Private original receipts remain under
`/home/tux/secure-handoff/scientific-unattended-20260919/v52-owned-worker-eviction-r2/`
and `v52-owned-worker-recovery/readback-01/`. Do not relabel the failed study as
successful after this source change.

## Repair

Single-Pod scientific Jobs now receive an ordered `podFailurePolicy`:

1. `FailJob` for a nonzero exit other than SIGTERM143, including OOM/ambiguous137.
2. `FailJob` for a true `DisruptionTarget` Pod condition.

The second rule leaves a durable, controller-generated failure reason on the
Job even after the Pod is removed. The observer recognizes only the exact owned
policy, namespace, Job-derived Pod name, terminal condition and matching rule.
An intermediate `FailureTarget` is not prematurely classified as an application
failure. Existing observed OOM, execution timeout and model-exception evidence
retains precedence. Unrecognized formats remain conservative application
failures. This does not add JobSet coverage or prove actual cloud preemption.

Both actions fail the current Job; neither uses `Ignore` or adds a hidden
Kubernetes retry. `backoffLimit: 0`, the frozen stage attempt budget, accounting,
idempotency, resource limits, and execution deadlines are unchanged. SIGKILL137
without retained Pod evidence remains intentionally ambiguous/non-retryable.

The policy is stable since Kubernetes1.31; the target cluster reports1.35.6.
Primary references: [Kubernetes failure policies](https://kubernetes.io/docs/tasks/job/pod-failure-policy/)
and [v1.35 controller implementation](https://github.com/kubernetes/kubernetes/blob/v1.35.0/pkg/controller/job/pod_failure_policy.go).

## Qualification boundary

Source regression covers missing Pods, exact policy matching, wrong namespace or
Job/rule, application/OOM/timeout precedence and intermediate failure status.
Server-side dry-run and a newly admitted, exactly owned live eviction/recovery
must be recorded separately before claiming deployment or live recovery.

On19September at17:24UTC,142 focused adapter/controller/production tests passed
(one pre-existing Starlette deprecation warning), and Ruff passed. A suspended,
CPU-only Job carrying the exact policy passed `kubectl create --dry-run=server`
against the target1.35.6 API server; no Job or Pod was created by that check.
The test policy retains `backoffLimit: 0` and uses no `Ignore` rule.

## Deployed190 and fresh live recovery

Helm190 deployed source`c154d77654a16b5cfdf732f505467ac981b6479f`, index
`sha256:e4319840c1d9378c2942f2e389785917e045af4b5733f93da3b535c3f33e3dad`.
Gateway3/controller2 replicas are ready; the existing admin image remains
`sha256:72581f9f4035742e8c0b52a197c9b9a702f17fe8a6a837990dd2f7bf42ce4ae4`.
Live Helm values/manifest comparison changed only the control-plane image;
no model policy, capacity, quota, deadline or retry budget changed. Follow-up
API reads returned200 for the37-App operator catalog, retained accounting and
new recovery accounting; Cellpose/scVI and existing model entries are present.

The separately labelled RF48-residue/seed1 qualification operation
`2a1bc85e-b31f-479d-a245-6ea16b112d2f` ran17:34:16–17:36:40UTC. Exactly one
owned Pod was evicted at17:34:38; watch evidence retained the Job's exact
`PodFailurePolicy`/rule1 terminal reason. The application observer classified
attempt1 as `infrastructure/JobDisruptionTarget`. Attempt2 completed, both GPU
reservations and the CPU collector reservation were released, and two artifacts
passed result validation and hash readback. The unchanged policy allows two
attempts. Idempotent replay returned the original operation/workload, not new work.

`verify_recovery.py` independently confirms the terminal Job reason, accounting
and identity invariants, plus192 finite atoms/48 CA residues and adjacent CA
distances3.686–3.773Å. PDB SHA256:
`043cde973829934b39e94f441052d14fd3708abcbb0f3a008f4ffbcfb70fbc5d`.
This is coarse integrity and maintenance-eviction recovery, not a biological
efficacy test, whole-node outage or provider-preemption qualification.

Protected evidence directory: `worker-eviction-r190-rfdiffusion` under the
private run root cited above. Independent receipt SHA256:
`32191728cf392bd688fd69ad881c0aee9dc697805ac424ada8d65db34f608920`;
original recovery receipt SHA256:
`704b95ed70a8425a46649c63fae337fbb48c21addb3e1ff593094de1a003f64b`;
Job watch SHA256:
`68bed9f111f870e89791f8084ab52902f6c00dac4fc753ad61bbe2b5f1862c8f`.
The original failed v52 natural study remains failed. This separate test does
not claim that its full RF→ProteinMPNN→ESMFold customer workflow has passed.
