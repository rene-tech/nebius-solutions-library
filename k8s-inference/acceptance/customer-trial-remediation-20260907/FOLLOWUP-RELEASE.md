# Follow-up release after r01

Source: `5f5061b28ee71a59432492a1bdf6106428a85367`, pushed to
`rene-tech/nebius-solutions-library` main. Source tree:
`56106b59a8d3b67ac54c84204507119104063227`.

Status: deployed through the staged Terraform workflow at20:05:51UTC on
2026-09-07. At20:05:55UTC all three application Deployments had the expected
images, observed generations and two updated/ready/available replicas, with no
terminating replicas. Post-apply infrastructure, foundation and workloads plans
all have zero managed actions. Public discovery/admin inventory again passes
all24 required configured models. Complete r02 started20:10:47.879537UTC with
unchanged14 scenarios and separate ordinary HTTP/MCP, cluster and browser lanes.
R03 has not started; no complete clean full cohort is claimed yet.

The first live browser preflight failed before sign-in: an empty page never
rendered the bootstrap-token field within30s, without a recorded pageerror or
HTTP error. It did not exercise phase durations or downloads. The failed private
receipt `experience/phase-followup-r01/report.json` is preserved; a bounded
read-only diagnostic is investigating browser initialization/assets/network.
Fresh public HTML and its JS asset return200 with the correct JS MIME type;
that alone does not establish browser usability. This failure is not silently
retried or presented as a passing phase check. A separate fresh diagnostic at
20:08:11–12UTC rendered sign-in in about1s with the correct new JS/CSS, expected
session401 and no pageerror/requestfailed. There is no evidence of a persistent
asset defect; the original transient cause remains unknown.

After explicit authorization, a fresh bounded `phase-followup-r02` passed at
20:09:14.558–20:09:18.356UTC. API restore3.946846seconds/estimated, browser3.95s,
both timestamp labels and the wall-time-union explanation were correct. The
browser downloaded and verified32,796bytes of mmCIF in0.405s; SHA256:
`55d5f96024aafba3d0be88fd02fabf272f5fee7209b453fd0a7bf8e941c37e24`.
GPU partition remained33occupied=16.053154active+16.946846idle, delta0. Only the
expected initial session401 occurred; no pageerror/requestfailed. Browser closed
20:09:18.412UTC. The earlier blank-page attempt remains preserved, not erased by
this passing check. The harness now retains bounded console/request-failure and
document/script/stylesheet metadata for any recurrence.

Private post-apply plan JSON SHA256s:

- Infrastructure: `76c2399a6f8c346c5779d4f490e5f83bd2f897af30b52833929bbfc5f7f71a8a`.
- Foundation: `b85d0c7500d5c1bedae689b9876f7e06fca9b6ba8581341821096821f4a32f1d`.
- Workloads: `5660f5b37eee041b0dd94a2642c45fdb98049e99abbe7a753d6947db03ab4cf3`.

The first plan attempt hit a provider-download TCP reset during workloads
initialization, after successful zero-action infrastructure and the expected
foundation contract replacement. No workloads plan was applied. Its private log
is retained as `5f5061b2-cohort-plan-provider-reset.log`; the same exact-source
workflow succeeded on retry without configuration or provider-version changes.
The approved plan had24 workload actions; every queue/flavor/priority manifest's
non-metadata content was unchanged. Infrastructure and foundation were zero-action
on the successful retry. Existing immutable bootstrap contracts/Jobs were replaced
by the ordinary release workflow; retained source can recreate them. No customer
results or persistent stores were removed.

| Component | Published digest |
|---|---|
| Control plane/model controller/scientific companions | `sha256:8090641d0cfe8c22411c1e72df125ca3c488c5b69087d0a2c7887d87fd55b23f` |
| Admin console | `sha256:400b6775d3dd98ac5163e3ec658aea1cca5f5c8802ff73a38dbac34966c36605` |

## Scope

Qwen serving status now tolerates the narrow HPA-to-ScaledObject idle
acknowledgement lag only when an independently converged fixed hot Deployment
is ready. Scaler/HPA ownership, target, generation, zero desired replicas and
relinquished replica-field ownership remain required. Bootstrap and all-cold
handoff remain strict. Thirteen negative/positive controls cover the boundary.

Admin phase durations use paired lifecycle occurrence timestamps and per-phase
wall-time unions, not controller-event ingestion timestamps or a conversion
from GPU-seconds. Parallel/rank intervals count once, disjoint retries remain,
and missing boundaries are unavailable or explicitly incomplete. The retained
r01 Protenix fixture returns3.946846s even with gpu_count0; application-observed
quality remains explicitly estimated. The run's observation timestamp and the
separate cluster-context timestamp are labeled distinctly.

No runtime recipe, snapshot bundle, scientific fixture, model configuration,
resource request, pool limit, quota, priority, hot floor or driver changed. The
private customer tfvars changes only the two image pins and matching provenance.
The full exact repository archive includes shared Terraform modules.

## Verification

- Full non-external backend:1,611passed,4optional skips,76external-service tests
  deselected,222.09s. Existing Starlette deprecation and pytest temporary-directory
  cleanup warnings do not change the passing exit code.
- Independent focused routing/controller/publication/MCP:107passed.
- Admin backend:100passed including24 phase/populated isolated PostgreSQL and
  related tests; UI149passed; TypeScript, focused strict typing and Ruff passed.
- Runtime recipe identity check passed without refreshing qualification.
- Future observer uses complete bounded publication-log windows in addition to
  unchanged25s point samples. R01's academic Pod CPU/RAM metric coverage omission
  is corrected transparently, and only those two query filters gain the namespace.

The original failed cohort and r01 remain preserved. Neither successful unit
tests nor14/14 r01 science results substitute for clean live reruns.
