# MCP availability remediation

Implementation based on clean solutions-library `main` at `1b3b611832dab8ecf1315afb3e4162347c4431f1`.
Root owns the integrated commit, image build, deployment and live verdict. No live
mutation or extra model invocation was performed by this implementation lane.

## Failure and correction

The original campaign remains unchanged in `../customer-trial-20260907`.
Qwen MCP request 46 started at 15:12:02.515582Z and failed after 0.96245 seconds.
At 15:12:03.426Z CP Pod `fs2-serve-control-plane-6c7fdcc757-pwmqv` logged
`RuntimeError: model is not routable`; its HTTP transport nevertheless returned
200. The original client captured only the outer `ExceptionGroup`.

Retained cluster samples show burst Pod
`qwen3-8b-b300-burst-h100-1x-6d6575886f-x7td5` created and scheduled at 15:11:39Z,
still not initialized at 15:12:04Z. The original hot Pod remained Ready. The
reproduced source-level failure is:

1. Model readiness required **all** hot and burst Deployment rollouts complete.
   A scheduled but initializing burst demoted the whole model to `Localizing`,
   despite a current, completely observed hot Deployment serving traffic.
2. The controller emits `Loading=True` for `Localizing`, but publication expected
   `Progressing=True`. That otherwise valid activatable state lost its route.
3. Generic MCP lookup let the registry's `RuntimeError` escape as an unexpected
   SDK tool failure, without actionable correlation or retry classification.

The correction separates serving availability from full elastic convergence:
one exactly observed, fully ready Deployment may keep the model Ready while
another bounded segment scales. The complete desired resource ownership/digest
checks and autoscaler handoff checks remain mandatory. A stale replacement hot
rollout is not accepted as Ready. `Localizing` now uses its actual `Loading`
condition, including the same observed-generation check; it remains activatable,
not falsely ready, when no hot runtime exists.

Only the registry/admission's typed availability error is converted into a
standard MCP tool error (`isError=true`, structured `error.type`, `retryable` and
generated `request_id`). Lookup and the second admission-refresh race are both
covered. Unknown/unauthorized models retain policy denials; arbitrary runtime
bugs are not mislabeled retryable. The request ID appears in a payload-free
server log. Dynamic publication decisions log phase/source resource version only
when their revision/disposition/reason changes, so a later withdrawal can be
correlated without reconstructing it from HTTP 200 counts.

Evidence limitation: the historical PostgreSQL reporting query was denied with
SQLSTATE 42501. No new permissions or alternative credentials were used. The
exact persisted controller observation from that instant is therefore not
claimed recovered; retained Pod/log evidence and deterministic regressions
support the mechanism. Live recurrence testing is the remaining acceptance gate.

Private evidence: existing H100 acceptance directory
`releases/trial-customer-20260907/observer/qwen-mcp-exception-loki-r01.json`, SHA256
`956e2d94a395791dc71e45682b27a69bbe85be494c6936d65dbdfd42fd0e733d`;
the original sampler's `samples.jsonl` retains the Pod observations.

## Offline verification

From `components/control-plane`:

```sh
.venv/bin/python -m pytest -q tests/test_model_route_availability.py tests/test_model_deployment_controller.py tests/test_model_deployment_publication.py tests/test_model_deployment_bridge.py tests/test_api_mcp.py tests/test_registry_contract.py tests/test_scientific_mcp_aliases.py
```

104 passed in 38.22 seconds; one existing Starlette deprecation warning. Ruff
check passes on all changed MCP/controller/bridge/registry source and tests.
The new MCP regressions use a real Streamable HTTP client against the ASGI app,
and test `isError`, structured content, correlation, empty admission storage and
no unexpected-error logging. Broader deployment acceptance is not inferred from
these offline checks.

## Repeat-campaign monitoring (prepared, not yet started)

New scripts under this directory preserve the historical harness and receipts:

- `observer/sample_cluster.py`: the existing 25-second sampler, extended to all
  Kueue namespaces, academic Pod CPU/RAM, disk available/size bytes and the exact
  Qwen ModelDeployment status. Missing metrics remain missing, never zero.
- `experience/interactive_sampler.py`: same three synthetic Qwen checks, one
  client, 25-second cadence, alternating HTTP and MCP, no submission retries.
  It now retains bounded nested exception types, HTTP/protocol codes, per-call
  MCP `isError`, allowlisted domain error fields and request IDs. It does not
  print exception text, credentials, signed URLs or payload-bearing tracebacks.
- `experience/test_diagnostics.py`: offline checks for nested failure fidelity,
  correlation, truncation visibility and exclusion of secret exception text.

Use a fresh private `releases/trial-customer-remediation-20260907/<cohort>/observer`
and `<cohort>/experience` for **each** full campaign. Pass the existing private
`run/final-stack-output.json` to `--credential-bundle` (observer) or `--credentials`
(interactive). The observer additionally requires the explicit retained H100
`--kubeconfig` and `--context k8s-inference-h100`.

Begin only after root confirms the integrated deployment. Record baseline,
`during`, and `after` phase changes in the experience `phase.json`; stop using
its `stop.json` only after final recovery. Stop the observer with SIGTERM after
at least three recovery cycles and require both `sampler-completed.json` receipts.
Root decides cohort success from every scientific operation, normal client
request and UI check; no failure is removed or reclassified as a pass.

## Separate bounded Qwen burst regression

`experience/qwen_burst_regression.py` is prepared but must not run until root
authorizes the integrated deployment's test window. Run it separately from the
normal 25-second sampler, so total Qwen concurrency does not exceed three.
It uses the same arithmetic/translation/sorting fixtures via public HTTP/MCP,
with three concurrent clients per wave, at most 12 waves/36 requests, ten-second
wave spacing, and no new admissions after 120 seconds. Existing per-request
status deadlines finish the final wave; no inference retry is performed. Any
failed call stops further waves. Thirty seconds of recovery observation follow.

Only read-only `kubectl get` queries are used, with explicit kubeconfig/context,
for Qwen Pods and its ModelDeployment. Two-second observations correlate a
scheduled, not-yet-initialized burst Pod with original hot UID readiness,
ModelDeployment phase, and overlapping public request intervals. The summary
separates baseline burst UIDs from newly observed ones and does not claim causal
attribution from timing alone. It verifies unchanged desired-spec hashes and
reports remaining nonterminal or unknown operation IDs without deleting them.

If normal bounded requests do not produce an observable transition/overlap,
the result is `coverage-not-observed`, never a transition PASS. Do not change
thresholds, hot floors, capacity ceilings or policies to manufacture coverage.
Campaign transitions plus the offline regression remain complementary evidence.
Dedicated records are `plan.json`, `requests.jsonl`, `observations.jsonl` and
`summary.json` in a new private directory, never mixed into the normal sampler's
availability or latency distributions.

Preparation-only checks: `--help`, Python compilation, Ruff, and seven offline
verdict tests covering real overlap, missing coverage, hot loss, request failure,
old Localizing demotion, preexisting bursts and missing/changed observations.
