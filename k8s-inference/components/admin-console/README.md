# FS2 Serve admin console: inventory and vertical-slice contract

This directory contains the sealed design input and a task-owned read-only
operator-console preview. The user accepted the proposed React/TypeScript/Vite
stack on 2026-08-30. The preview implements the shared shell, Overview, Models,
Model detail, Operations, and Operation detail against the versioned same-origin
BFF. Production session/RBAC, image/Helm packaging, and retained rollout remain
separate acceptance gates.

The recommended first implementation is deliberately small: an authenticated
same-origin admin BFF plus Overview, Models, and Model detail pages. The BFF
joins the durable PostgreSQL ledger, catalog identity, current Kubernetes state,
and bounded Prometheus data. The browser never receives Kubernetes, database,
Prometheus, Loki, or cloud credentials.

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

## Gates before retained rollout

1. Define browser authentication and operator RBAC. The current single
   bootstrap admin bearer token must not be placed in browser storage or
   JavaScript. Recommendation: existing gateway authentication plus an
   HTTP-only same-site session exchanged by the BFF, with viewer/operator/admin
   roles mapped server-side.
2. Reconcile and package the read-only FastAPI projection, static image, Helm
   route, and Terraform release as one retained rollout with an explicit
   rollback target.
3. Supply a licensed Nebius asset package or explicitly approve the neutral FS2
   wordmark. Do not scrape visual tokens or copy public assets into source.

The neutral functional shell and source-only browser acceptance do not depend
on those retained-rollout gates.
