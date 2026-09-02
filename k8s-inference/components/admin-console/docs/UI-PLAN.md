# UI route and component plan

## Application shell

All pages live below `/admin` and share five stable regions:

```text
+----------------------+-----------------------------------------------------+
| ProductRail          | ContextBar: project / cluster / region / time / TZ |
| FS2 Serve            +-----------------------------------------------------+
| Overview             | Breadcrumbs + page title + freshness + actions     |
| Models               +-----------------------------------------------------+
| Model deployments    |                                                     |
| Operations           |                                                     |
| Users & API keys     | Responsive page content                            |
| Capacity & queues    |                                                     |
| Observability        |                                                     |
| Configuration        |                                                     |
| Audit                |                                                     |
+----------------------+-----------------------------------------------------+
```

`AppShell` owns navigation, focus restoration, skip links, route errors, and
the selected context. `ContextBar` options come from
`GET /admin/api/v1/context`; arbitrary project, region, cluster, or GPU-family
constants are forbidden in components. `DataBoundary` renders loading, empty,
error, stale, partial, forbidden, and unsupported states. Missing data renders
an em dash plus a reason, never numeric zero.

Desktop (at least 1200 CSS pixels) uses a 240-pixel product rail, 56-pixel
context bar, 24-pixel content gutters, and a 12-column content grid. Tablet
(768–1199 CSS pixels) collapses the rail to a 64-pixel icon strip, stacks
summary panels, preserves the context controls in a drawer, and permits
horizontal scrolling only inside resource tables. At either width, the first
resource column and row actions remain keyboard reachable. These dimensions
are FS2 layout choices, not claimed Nebius design tokens.

## Routes and page composition

| Route | Page | Required components | BFF contract |
|---|---|---|---|
| `/admin` | `OverviewPage` | `FleetHealthTiles`, `ModelStateSummary`, `DemandAndLatencyChart`, `QueueCapacityPanel`, `AlertAndChangeFeed` | `GET /admin/api/v1/overview` |
| `/admin/models` | `ModelsPage` | `ModelFilters`, `ModelResourceTable`, `HotnessChip`, `FreshnessBadge` | `GET /admin/api/v1/models` |
| `/admin/models/:modelId` | `ModelDetailPage` | `ModelIdentityHeader`, `RuntimeStatePanel`, `PerformancePanel`, `ReplicaAndPlacementTable`, `SnapshotCachePanel`, `SemanticHealthPanel` | `GET /admin/api/v1/models/{model_id}` |
| `/admin/model-deployments` | `ModelDeploymentsPage` | `ModelDeploymentFilters`, `ModelDeploymentResourceTable`, `ObservedStateChip`, `DeploymentPagination` | `GET /admin/api/v1/model-deployments`, `GET /admin/api/v1/model-deployments/{name}/status` |
| `/admin/model-deployments/new` | `ModelDeploymentWorkspacePage` | `QualifiedModelSelector`, `ModelDeploymentForm`, `ValidationDecisionPanel`, `RenderPlanPanel`, `MutationCapabilityGate` | `GET /admin/api/v1/model-deployments:capabilities`; `POST` validate-preview, plan-preview, and apply routes |
| `/admin/model-deployments/:deploymentName` | `ModelDeploymentWorkspacePage` | `ModelDeploymentForm`, `RuntimeStatusPanel`, `RevisionHistory`, `ValidationDecisionPanel`, `RenderPlanPanel`, `MutationCapabilityGate` | `GET` desired revision, history, status, and capabilities; `POST` validate-preview, plan-preview, apply, drain, rollback, and reconcile routes |
| `/admin/operations` | `OperationsPage` | `OperationFilters`, `OperationResourceTable`, `TimingBreakdown`, `ErrorClassChip` | `GET /admin/api/v1/operations` |
| `/admin/operations/:operationId` | `OperationDetailPage` | `OperationIdentityHeader`, `OperationTimeline`, `AttemptTable`, `ContextualObservabilityLinks` | `GET /admin/api/v1/operations/{operation_id}` |
| `/admin/access` | `AccessPage` | `PrincipalTable`, `ApiKeyTable`, `ScopeSummary`, `UsageAttributionPanel` | `GET /admin/api/v1/principals`, `GET /admin/api/v1/keys` |
| `/admin/capacity` | `CapacityPage` | `GpuPoolTable`, `CapacityTypeChip`, `QueueTable`, `PendingWorkloadTable`, `AutoscalerStatePanel` | `GET /admin/api/v1/capacity` |
| `/admin/observability` | `ObservabilityPage` | `ComponentHealthGrid`, `ContextualLaunchCard`, `UnavailableCapabilityNotice` | `GET /admin/api/v1/observability` |
| `/admin/configuration` | `ConfigurationPage` | `ConfigurationResourceTable`, `ProposedDiff`, `ValidationResults`, `ReconciliationTimeline`, `RollbackTarget` | `GET` configuration and reconciliation status; `POST` diff, validate, plan, reconcile, and rollback routes |
| `/admin/audit` | `AuditPage` | `AuditFilters`, `AuditEventTable`, `AuditDetailDrawer` | `GET /admin/api/v1/audit` |

Configuration and ModelDeployment routes are feature-gated by the corresponding
server-side services. ModelDeployment mutation controls additionally require
the writer capability; the UI fails closed when capabilities are absent. The
exact route groups and disabled behavior are sealed in
`contracts/admin-api-v1.json`.

### Qualified Add Model defaults

The create workspace never asks an operator to discover or copy artifact,
runtime-image, or renderer-template digests. Its `Qualified model` selector is
populated only from `configuration_options` in the mutation-capabilities
response. Every option is tied to `configuration_revision`, contains a default
`ModelDeploymentSpec` already accepted by the installed
`InfrastructureEnvelope`, and restricts pool, queue, priority-class, and tenant
choices to that envelope. Missing, malformed, unpinned, or unplaceable tuples
are omitted, and an unavailable or empty capability response leaves creation
disabled without a browser-generated fallback.

Selecting another qualified model replaces the server-owned model/artifact/
runtime/template/placement/cache tuple. Operator policy remains editable:
hot floor and ceiling, queue priority, rollout, OpenAI/MCP publication,
visibility, and allowed principals. A zero hot floor is offered only when the
exact option declares `scale_to_zero_qualified=true`.

Every filter is URL-addressable. Shared query keys are `project`, `cluster`,
`region`, `from`, `to`, and `timezone`; page-specific filters append stable
keys such as `model`, `status`, `principal`, `operation`, `gpu_class`, and
`capacity_type`. Server-returned cursors are opaque and are not reused across a
changed filter set.

## Exact first vertical slice

The first slice implements only the shell, Overview, Models, and Model detail.
It is read-only and introduces these typed projections:

- `GET /admin/api/v1/context` — allowed projects/clusters/regions, default time
  range, timezone choices, operator role, and capability flags.
- `GET /admin/api/v1/overview` — fleet counts with data-quality state, bounded
  request rate/latency/error summaries, durable totals, queue state, capacity
  summary, and recent audit changes.
- `GET /admin/api/v1/models` — cursor-paginated catalog identity joined to
  observed status, status reason, evidence time, replica/queue summary, immutable
  image/model identity, supported GPU classes, and explicitly nullable metrics.
- `GET /admin/api/v1/models/{model_id}` — the list projection plus endpoints,
  MCP exposure, runtime variant, activation evidence, placement, snapshot/cache
  metadata, semantic health, and metric summaries.
- `GET /admin/api/v1/observability` — only verified components,
  health, freshness, and runtime-generated launch URLs. A component with no UI
  or failed discovery is disabled with a reason.

The BFF response envelope is consistent:

```json
{
  "meta": {
    "schema_version": "fs2.admin-api/v1",
    "generated_at": "RFC3339 timestamp",
    "context": {"project": "...", "cluster": "...", "region": "..."},
    "sources": [],
    "warnings": []
  },
  "data": {}
}
```

Unknown and unavailable numeric values are JSON `null` with a bounded reason
code, not `0`. Error responses use a request ID and stable problem type; they
never include SQL, Kubernetes objects, prompts, responses, tokens, or secrets.

## Model status contract

Status is a BFF projection, not a browser guess. It includes `status`,
`reason_code`, `observed_at`, and source freshness. Precedence is:

1. `unsupported` — the selected runtime/GPU compatibility decision is a fresh,
   explicit rejection.
2. `unknown` — a required adapter is unavailable or stale beyond its contract.
3. `unhealthy` — rollout timeout, failed semantic health, or explicit runtime
   health failure.
4. `loading` — activation is claimed/in progress or desired replicas exceed
   ready replicas inside the startup window.
5. `hot` — at least one desired replica is ready and representative semantic
   health passes.
6. `queued` — demand or an activation intent is queued while no replica is
   loading or ready.
7. `cold` — supported, fresh, no active/queued activation or demand, and zero
   desired/ready replicas.

The live deployments currently lack an activation-state label, so desired and
ready replica counts alone are insufficient to call a model hot. The adapter
must join the PostgreSQL activation ledger and semantic-health evidence.

## Observability launches

Launch URLs are runtime configuration returned by the observability projection;
none are committed here. Grafana, Prometheus, and Loki are verified and may be
enabled. The selected cluster/model/operation/time context is encoded only in
allow-listed variables or query templates. OTel has no operator UI. DCGM is
deployed but its series are not currently ingested. Kueue has a visibility API
but no verified operator UI. Alertmanager and Tempo are absent. All five remain
disabled until their individual discovery checks pass.

## Interaction and accessibility acceptance

- A keyboard user reaches rail, context, title actions, table controls, rows,
  drawers, and pagination in a predictable order; opening and closing a drawer
  returns focus to its trigger.
- Status never relies on color alone. Chips include text and an accessible
  reason; charts have a table alternative.
- Route, filter, sort, page, and time context survive reload/back/forward.
- Skeletons preserve layout, empty is distinct from unavailable, stale shows
  the last evidence timestamp, and a partial-source failure does not erase
  unaffected data.
- Desktop and tablet browser acceptance checks overflow, visible focus, target
  size, heading hierarchy, label association, and no uncaught console errors.

## Remaining ownership boundary

The initial read-only slice has since been extended with operations, access,
configuration, and ModelDeployment workflows. Onboarding a new catalog model,
adding accelerator pools or queues, and changing Terraform-owned infrastructure
remain outside live admin mutation. Those changes must first update the retained
qualification and `InfrastructureEnvelope`; the UI never fabricates evidence or
patches generated Kubernetes resources directly.
