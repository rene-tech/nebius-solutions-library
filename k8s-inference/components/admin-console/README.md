# FS2 Serve admin console

This directory contains the React/TypeScript operator console and its sealed
design inputs. The console implements Overview, Models, Model detail,
Operations, Operation detail, Users and API keys, Capacity and queues,
Observability, Configuration, and Audit against the versioned same-origin BFF.
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

No Nebius visual asset exists in the repository. The official
[Nebius trademark guidelines](https://nebius.com/brand-assets/trademark-usage-guidelines)
require permission for brand-asset use and prohibit modification or implied
endorsement. Therefore this contract uses a neutral `FS2 Serve` wordmark and
does not copy the public logo. A licensed, repository-owned asset may replace
the wordmark only after its approval and provenance are recorded.

The information architecture uses verifiable console vocabulary, not a claimed
Nebius design system. Official Nebius documentation places metrics behind
Observability, provides time filters and resource Metrics tabs, and documents
Administration/IAM roles:

- [Service dashboards](https://docs.nebius.com/observability/dashboards)
- [IAM roles](https://docs.nebius.com/iam/authorization/roles)

The rail, dense resource tables, status chips, and responsive measurements in
this directory are FS2 product choices. They are not represented as official
Nebius UI tokens.

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
4. Supply a licensed Nebius asset package or explicitly approve the neutral FS2
   wordmark. Do not scrape visual tokens or copy public assets into source.

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
