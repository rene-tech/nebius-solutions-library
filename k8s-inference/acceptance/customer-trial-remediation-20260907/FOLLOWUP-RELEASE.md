# Follow-up release after r01

Source: `5f5061b28ee71a59432492a1bdf6106428a85367`, pushed to
`rene-tech/nebius-solutions-library` main. Source tree:
`56106b59a8d3b67ac54c84204507119104063227`.

Status: images published; staged Terraform plan in progress. Not yet deployed.

The first plan attempt hit a provider-download TCP reset during workloads
initialization, after successful zero-action infrastructure and the expected
foundation contract replacement. No workloads plan was applied. Its private log
is retained as `5f5061b2-cohort-plan-provider-reset.log`; the same exact-source
workflow is being retried without configuration or provider-version changes.

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
