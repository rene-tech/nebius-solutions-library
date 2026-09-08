# Managing inference apps

The admin console groups day-to-day work into **Apps**, **Users** and **Capacity**.
The original diagnostic pages and observability links remain under Advanced.
This guide describes the Apps implementation; the dated acceptance report records
which release and live workflows have actually been verified.

## Apps and models

An app is one independently configured deployment or batch execution target.
`model_ref` identifies its qualified model source; `app_id` is its permanent
console identity; `public_model_id` is its inference route. Two apps can use the
same source model and cached artifacts while having separate routes, settings,
requests and usage. Existing model routes and run IDs remain valid.

Apps remain listed at zero replicas. The list's run count obeys the selected
time window; **Last used** is retained across windows. A quiet hour does not mean
the app has never served requests. Choose a longer window to inspect earlier work.

Each app provides:

- **Runs:** one entry per logical operation. Scientific stages, retries, artifact
  publication and downloads enrich that same run. Actual HTTP exchanges/MCP tools
  appear separately; polling and idempotent replays are not new model runs.
- **Metrics:** time-series CPU, RAM, concurrency, running/ready containers, GPU
  utilization and GPU memory. Charts state their units and aggregation. GPU
  samples are associated with observed device-allocation intervals, not every GPU
  on the worker node. Unobserved intervals are gaps, not invented zeros.
- **App Logs:** retained Loki logs from the app's current and historical Pod
  identities, with search and container filters. An operator can inspect a
  completed job after Kubernetes removes its Pod, while retained logs exist.
- **Containers:** actual container instances, readiness, restarts, state, node,
  image and requested accelerator resources. Pending/init containers are visible;
  completed jobs remain in Runs rather than pretending to be running containers.
- **Usage:** logical runs, users, token counts and available lifecycle accounting,
  plus separately observed HTTP traffic, response classes and response durations.
  Historical byte/HTTP transport details unavailable before instrumentation are
  explicitly unknown. An MCP HTTP200 can still carry a tool error.
- **Settings:** app name, academic classification, actual runtime controls and
  compatible startup options. Save applies through the control plane; it does not
  generate a Terraform instruction for ordinary model scaling.

## Saving settings

Serving apps expose minimum/maximum reusable workers, idle/startup retention,
queue target and qualified caching/snapshot options. Save uses the current app
revision and deployment ETag. If another operator has edited it, reload before
retrying rather than overwriting their settings. Desired state and the
controller's observed state are separate: a successful Save is not proof that a
new worker is Ready yet.

Job-based scientific apps expose pause/resume, maximum active runs and their
qualified per-stage startup options. Maximum active runs is a dispatch limit,
**not** a minimum hot-worker count. Such an app does not claim reusable workers
unless its runtime implements them. Existing accepted runs retain their frozen
execution policy. Selecting a GPU snapshot is a requested policy; Runs/lifecycle
evidence determines whether a particular attempt actually restored it.

Terraform owns infrastructure, installed platform services and the initial
model configuration. App registration is create-only during initialization:
restarts must preserve saved app names, identities and settings. Infrastructure
expansion still respects the Terraform-defined pool boundaries.

## Users and capacity

A User is an inference owner, distinct from a console administrator. Usage joins
all keys belonging to `(tenant_id, principal_id)`, including rotated keys. App
allowlists use app identities, so permission for one deployment does not silently
grant its sibling. Academic eligibility supplements existing model/license and
key rules; it does not replace them. Disabling a user stops new invocation while
allowing access to already accepted work. Keys use the existing issue, rotate and
revoke operations; save newly issued values when disclosed.

Capacity separates configured pool limits, current nodes, GPU reservations,
measured utilization, loaded-idle serving workers and schedulable-free resources.
Zero-node pools can still show their configured expansion headroom. Headroom is
not a promise of available cloud supply. Logical pending runs are distinct from
physical queue workloads/shards. A free GPU count alone does not guarantee that
the model fits its GPU memory, node CPU/RAM, taints and placement rules.

## Timing and accounting

Do not compare a restore command's duration with end-to-end cold-start latency.
Queueing, node provisioning, image pull, process startup, weight loading/restore,
execution and result publication are different intervals. Per-run clocks keep
their existing lifecycle meaning; sums over a window are not benchmarks.

Scientific GPU occupancy is reported from complete reconciled attempt records,
with active and idle time separate. Online shared-worker overhead is not charged
once to every concurrent user. Where exclusive attribution is unavailable the
console says so, rather than presenting a billing-quality figure.

The API contract is documented in
[`admin-api-v1.json`](../components/admin-console/contracts/admin-api-v1.json).
Primary routes start at `/admin/api/v1/apps`, `/admin/api/v1/users` and
`/admin/api/v1/capacity/summary`; they use the existing same-origin operator
session. Cluster, database and observability credentials stay server-side.
