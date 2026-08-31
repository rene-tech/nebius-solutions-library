# ADR-0001: frontend stack and delivery boundary

- Status: Accepted for the task-owned preview; retained rollout still requires release reconciliation
- Date: 2026-08-30
- Scope: FS2 Serve admin-console UI only

## Context

The consolidated repository uses Python/FastAPI, JSON-schema/catalog contracts,
Helm, and Terraform. It has no JavaScript runtime, lockfile, component library,
browser test setup, or owned visual token package. The console needs dense
interactive tables, URL-addressable filters, tablet/desktop layouts, accessible
drawers/dialogs, and a typed same-origin BFF. The cluster should not need a Node
runtime to serve the compiled UI.

Official Nebius documentation supports the surrounding navigation vocabulary
(Administration/IAM, Observability, resource metrics, time filters), but no
repository-owned reusable design-token source was found. The public trademark
guidelines require permission for brand assets. Therefore framework selection
must not depend on copying the logo, CSS, or artwork.

## Proposed decision

Use a strict TypeScript single-page application with:

- React for readable component composition and mature accessible primitives;
- Vite for a small static build with no Node runtime in production;
- React Router for URL-owned route/filter state;
- TanStack Query for bounded server state, cancellation, freshness, and retry;
- types generated from the FastAPI OpenAPI document, with runtime validation at
  the BFF boundary for fixtures and compatibility tests;
- CSS Modules plus an FS2-owned semantic token layer; no copied Nebius CSS;
- Vitest and Testing Library for components/contracts;
- Playwright plus an accessibility scanner for desktop/tablet acceptance;
- npm with a committed lockfile, exact dependency versions, and a pinned Node
  major recorded in the scaffold.

Build one non-root static web image. Route `/admin` and its immutable assets to
that image and `/admin/api/v1` to the existing FastAPI control plane through the
same Envoy authority. The BFF owns browser session/RBAC, infrastructure
credentials, joins, query bounds, redaction, and deep-link signing. The browser
never calls Kubernetes, PostgreSQL, Prometheus, Loki, OTel, or cloud APIs.

The initial semantic tokens are product-neutral roles such as
`surface-default`, `surface-raised`, `text-primary`, `text-muted`, `border`,
`focus`, and status roles. Their literal colors are selected by the frontend
child with contrast receipts. Nebius-branded colors/assets may replace approved
roles later from a licensed repository package without changing components.

## Why this fits the repository

- The output is an immutable artifact that Helm can pin by digest like current
  platform images.
- OpenAPI generation reuses the existing typed FastAPI contract rather than
  defining a second schema language.
- Components, adapters, and browser acceptance remain isolated from the durable
  control-plane domain logic.
- GPU, region, cluster, and observability choices remain runtime data, not build
  constants.
- A small explicit dependency set and committed lockfile make the new language
  boundary reviewable.

## Alternatives considered

### FastAPI + Jinja/HTMX

This adds the least build infrastructure and is attractive for forms, but the
dense resource tables, preserved multi-page filters, streaming freshness, and
client-side observability context would accumulate ad hoc JavaScript. It also
couples frontend delivery to control-plane rollouts. Reject for the full console;
it remains viable for a temporary internal read-only page.

### Next.js

It provides routing and server rendering, but introduces a second production
server/BFF and risks splitting authorization and data joins from FastAPI. Static
export removes much of its advantage. Reject unless a repository-wide web
platform standard adopts it.

### Web Components with no framework

This minimizes dependencies but would require locally building router, query
cache, form, table, and accessibility conventions. Reject because it increases
custom platform code and review burden.

### Embed static assets in the FastAPI image

This gives the simplest same-origin routing, but couples UI releases and load to
the admission gateway. Keep as a fallback for the first demo only; the proposed
production boundary is a separate static image and shared Gateway authority.

## Approval and follow-up

The user explicitly requested this independent admin area on 2026-08-30 and
authorized continued implementation. The task-owned preview therefore adopts
Node 22/npm ownership in this directory, the neutral FS2 wordmark, and the
same-origin BFF boundary above. The access child still owns the production
HTTP-only session/RBAC contract, and the configuration/Terraform child owns
retained release wiring. Exact dependency versions, SBOM, image digest,
responsive screenshots, keyboard/focus receipts, and browser-console
cleanliness belong to the frontend child.
