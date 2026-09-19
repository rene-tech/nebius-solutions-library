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
