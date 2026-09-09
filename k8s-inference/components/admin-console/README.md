# Nebius Apps admin console

This directory contains the React/TypeScript operator console. The primary
navigation is Apps, Users and Capacity. Each independently identified app has
Runs, Metrics, App Logs, Containers, Usage and Settings. Existing operations,
scientific run IDs, model views and low-level diagnostics remain accessible
through their original routes and Advanced navigation.
It exchanges the cluster's admin bootstrap credential for a Secure, HttpOnly,
SameSite operator session and applies the server-published viewer, operator, and
administrator roles.

The BFF joins the durable PostgreSQL ledger, catalog identity, current
Kubernetes state, and bounded Prometheus data. The browser never receives
Kubernetes, database, Prometheus, Loki, or cloud credentials. Missing or stale
sources stay explicit: model support, cluster enablement, observed runtime state,
and metric availability are rendered as separate facts rather than collapsed
into a healthy or zero value.

## Design inputs

- [`acceptance/inventory.fixture.json`](acceptance/inventory.fixture.json) is a
  synthetic, non-operational inventory used to exercise the fail-closed UI
  contract without publishing cluster evidence or identifiers.
- [`contracts/admin-console-plan.json`](contracts/admin-console-plan.json) is
  the machine-checkable route, component, BFF, status, source, field, and
  observability-launch contract.
- [`docs/UI-PLAN.md`](docs/UI-PLAN.md) defines the exact shell, routes, pages,
  states, responsive behavior, and recommended implementation boundary.
- [`docs/DATA-SOURCE-MATRIX.md`](docs/DATA-SOURCE-MATRIX.md) maps each product
  question to its source of truth and records missing telemetry/schema rather
  than converting absence into zero.
- [`docs/PROVIDER-INSPIRATION.md`](docs/PROVIDER-INSPIRATION.md) records the
  provider-console patterns used for models, metrics, queues, access and usage,
  and the deliberate differences needed for heterogeneous FS2 workloads.
- [`acceptance/validate_plan.py`](acceptance/validate_plan.py) and the status
  fixtures are the pre-implementation acceptance gate.

## Visual and brand basis

The user-approved source is [Nebius communication design](https://nebius.atlassian.net/wiki/spaces/NEBIUSMARKETING/pages/643170998/Nebius+communication+design),
page version 52 (retrieved 2026-09-08), and its linked official Marketing Library
brand assets. The verified palette is deep blue `#052B42`, lime `#DAFF33`, violet
`#5D52F6`, lavender `#C1C1FF`, light blue `#F0F8FF`, and white. The official
[RGB logo](src/assets/nebius-logo.svg) is retained unmodified, including its own
background color; it is not a recreation. Internal guide PDFs are not bundled.

The guide uses Gramatika headings and Inter body text. No Gramatika font is
distributed here: headings/body use the existing Inter/system sans-serif stack.
The layout, charts and status presentation are application choices informed by
that reference, not a claim to ship an official Nebius component library.

Related official documentation:

- [Service dashboards](https://docs.nebius.com/observability/dashboards)
- [IAM roles](https://docs.nebius.com/iam/authorization/roles)

See [the approved Apps contract](../../docs/ADMIN_APPS_REDESIGN.md) for identity,
time-window, accounting and settings semantics. The Apps runtime API uses the
existing admin session; no infrastructure credentials enter the browser.
The opt-in [request debug viewer](../../docs/request-debug-logging.md) adds lazy
request/response inspection within Runs and a collapsed global log for rejected
requests without App attribution. See that guide for capture and retention limits.

## Develop and verify

```bash
cd k8s-inference/components/admin-console
./run_checks.sh
npm ci
npm run typecheck
npm run test:run
npm run build
```

The gate uses only the Python standard library. It validates contract shape,
route uniqueness, same-origin BFF isolation, absent-component handling,
GPU-agnostic fields, protected-resource exclusion, and all seven hotness states.

For a local fixture-backed browser preview only:

```bash
npm run build -- --mode fixture
npx vite preview --mode fixture --host 127.0.0.1
```

Fixture mode is local-only Vite middleware and is not enabled by the production
build. It must never be used as a deployment image.

## Retained rollout acceptance

1. Deploy the static image and same-origin BFF through the published HTTPS admin
   endpoint. Do not deploy the Vite fixture mode.
2. Confirm the runtime is wired to reviewed Kubernetes and Prometheus adapters,
   then verify real model states, operations, capacity, observability, users,
   API-key lifecycle, configuration handoff, and audit in a browser.
3. Confirm login with the admin bootstrap token, cookie renewal/expiry, role
   boundaries, logout, and correlated error messages. An inference or MCP API
   key is intentionally not an admin login credential.
4. Verify the approved unmodified logo, palette, readable charts/tables and
   responsive navigation in the integrated production build.

The unit route matrix uses backend-shaped envelopes and covers all
backend-integrated console routes, but it is not evidence of a live rollout.
Record the deployed image digest, endpoint, cluster context, and browser
acceptance separately.

The scientific run and model-readiness projection is documented in
[`docs/SCIENTIFIC-OPERATIONS-UI.md`](docs/SCIENTIFIC-OPERATIONS-UI.md). The
production control plane always registers the authenticated capability route;
model and run data routes are registered only when their real readers are
bound. Fixtures remain limited to component tests and the explicit local Vite
demo mode; production builds fail closed and never use them as fallback.
