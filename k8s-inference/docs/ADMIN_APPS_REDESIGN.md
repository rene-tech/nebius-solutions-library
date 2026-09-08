# Apps-first admin console

Status: implementation in progress, approved 2026-09-08. Baseline main
`306319a05f8b626d9eec9c20ffced4be00c92f71`. This document is a delivery plan,
not a claim that the features below are deployed or tested.

## Agreed product contract

- Primary navigation: Apps, Users, Capacity. Existing observability tools and
  low-level infrastructure diagnostics remain accessible under Advanced.
- An app is an independently configured deployment, not a model family.
  Two deployments of the same model are two apps with distinct identities,
  routes, settings, runs and usage. Initially every existing model/profile
  remains represented; existing public routes and run IDs remain valid.
- App tabs: Runs, Metrics, App Logs, Containers, Usage, Settings.
- Runs use the existing durable operation ID. Scientific details enrich that
  operation; they are not another counted run. Show endpoint/method or MCP tool,
  HTTP status separately from execution/result state, user, timestamps, queue,
  startup, execution and total response time. Preserve retries/stages/results.
- Metrics show real time series with explicit units and aggregation: concurrent
  requests, GPU utilization/VRAM, CPU, RAM and ready/running instances. Show
  average and peak, plus useful totals and latency p95. Missing is not zero.
- Logs are embedded, searchable and correlated with the actual app instances;
  containers expose status, readiness, instance/node, GPU, image and restarts.
- Usage shows requests, HTTP response classes, users, traffic and compute.
  Request polling/replays do not inflate logical inference counts. Resource
  occupancy, active execution, loading and allocated-idle time are distinct;
  shared idle overhead is not silently charged to each concurrent user.
- Settings save through the runtime control API, not Terraform. Desired state,
  applying state and actual readiness are distinguished. Preserve compatible
  cache levels and requested versus effective GPU snapshot state.
- Min replicas means ready reusable workers, never a renamed batch concurrency
  setting. Job-only apps expose honest capabilities until reusable workers are
  actually implemented and tested. Maximum active batch runs remains distinct.
- Users represent inference owners (people or service accounts); console roles
  are separate. Aggregate usage across keys and retain attribution through key
  rotation. User disable must affect invocation access, not only UI sessions.
- Academic eligibility and app classification are settings, not separate app
  kinds. Existing per-app assets/dependencies remain available in Settings.
- Capacity summarizes ready nodes/GPUs by type/pool, allocation versus measured
  utilization, loaded idle workers, schedulable-free GPUs, starting capacity,
  configured expansion headroom, pending customer runs and oldest wait.
  Configured headroom does not promise provider preemptible availability.
- Terraform provisions infrastructure and optional create-only initial apps.
  A later Terraform apply must not overwrite app settings saved in the UI.
- Adopt the user's Nebius communication design reference and a clear, compact,
  responsive operator layout. Keep actual metrics and readable tables central.

## Parallel ownership

All workers share the existing main worktree. No new branches, independent
deployments, bulk staging or commits. Root integrates, commits and deploys.

1. Apps backend: durable app identity, inventory and settings/routing integration,
   unified Runs, duplicate-model independence and regression tests.
2. Apps frontend/brand: navigation, app list/detail tabs, charts/components,
   app settings and UI tests. Coordinate DTOs directly with backend owners.
3. Users and Capacity: identity/usage/entitlement APIs, attribution correction,
   Users UI and simplified Capacity UI/API; tests.
4. Root: app metrics/logs/containers backend, shared API mounting/integration,
   source guidance, release, browser/live acceptance and handover.

Prefer new focused modules/router installers to simultaneous edits of api.py.
Root owns api.py integration. Existing public API behavior must be preserved.

## Verification and completion

- Unit and integration tests for app isolation, run deduplication, real timing
  semantics, user/key ownership and attribution, access updates and capacity.
- UI tests/build plus real-browser tests of every new tab, navigation, filters,
  charts and saved settings. No mock metrics in the production experience.
- Deploy the integrated exact source to the existing H100 cluster in
  project-e00rene/eu-north1 using the existing release workflow. Preserve prior
  images for rollback and previously working inference/scientific routes.
- Demonstrate two apps using the same model with independent identity/settings
  and traffic attribution. Use existing or preemptible capacity when needed;
  do not raise quotas or node-group limits or restart unrelated work.
- Verify live serving and scientific requests, run/log links, populated metrics,
  user usage, and a real reversible scaling/settings change from the UI.
- Record exact evidence and any limitations. Do not call incomplete tabs,
  unavailable adapters or unsupported settings finished.
- Leave the production cluster/portal running, stop temporary test workers,
  preserve meaningful source/evidence on main, and hand over usable URLs.

The user's current implementation scope supersedes older epic instructions
that would add unrelated security/audit work or repeated review gates. Slack
remains silent except one final outcome or a genuine user-input blocker.
