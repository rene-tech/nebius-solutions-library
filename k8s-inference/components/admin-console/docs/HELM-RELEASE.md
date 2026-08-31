# Admin console Helm release contract

The admin console is an optional static workload in the existing
`fs2-serve-control-plane` chart. It does not create a second edge, control
plane, credential store, or infrastructure owner.

## Source and image custody

The first sealed package source is commit
`76886d3398e48593282b19a30b49c065acebc2cd`, tree
`2ee85f8fabd1137a6f1fc5be68d7c4df2f041dfe`, with package-lock SHA-256
`4f3c6395f11ade5fb9ec55d18e7fa49cf632a02d774b9aa5219737e0f8b7ae4a`.
Its locally verified image ID is
`sha256:eb5dc8f72a3d8cc590a7ed843f44d579db8f0c66541ff63ee7c3ceb3d05144d6`.
That daemon-local ID is evidence only and must never be used as a registry
release digest.

The CycloneDX JSON SBOM was generated at
`<private-state-dir>/admin-console/sbom.cdx.json` with SHA-256
`fc323f6cf9d10b02b77e4df0908d2aba004ddbdad770427cc16d30e0ff7f59ef`.
Publish the exact-source image, retain its registry manifest digest and SBOM,
then supply all four immutable values at release time:

```yaml
adminConsole:
  enabled: true
  image:
    repository: REGISTRY/REPOSITORY
    digest: sha256:REGISTRY_MANIFEST_DIGEST
  provenance:
    sourceCommit: 76886d3398e48593282b19a30b49c065acebc2cd
    sourceTree: 2ee85f8fabd1137a6f1fc5be68d7c4df2f041dfe
    sbomSha256: fc323f6cf9d10b02b77e4df0908d2aba004ddbdad770427cc16d30e0ff7f59ef
    sbomFormat: cyclonedx-json
  httpRoute:
    enabled: true
```

`adminConsole.image.repository` accepts no tag or embedded digest. The chart
combines it only with the validated `sha256:` manifest digest.

## Routing and trust boundary

The existing TLS Gateway remains the only public listener. Gateway API
element-prefix precedence sends `/admin/api` and descendants to the control
plane and `/admin` and descendants to the static service. The static NGINX
configuration also rejects `/admin/api` as `application/problem+json`, so a
Gateway misroute cannot silently return the SPA.

The UI uses same-origin requests and stores no bearer, Kubernetes, PostgreSQL,
Prometheus, Loki, or Grafana credential. Operator session/RBAC must be present
in the reconciled control-plane image before public enablement. The Gateway
removes caller-supplied FS2 identity headers on both admin rules.

## Rollout order

1. Reconcile the read-only BFF, operator session/RBAC, frontend, chart, and
   current retained release into one reviewed source commit.
2. Build and publish exact-source control-plane and admin images; retain
   registry digests, provenance, SBOM, and scan receipts.
3. Render and review the complete Helm diff using the same values that the
   Terraform release workflow supplies. Confirm no unrelated model, capacity,
   Gateway listener, certificate, or Secret change.
4. Upgrade with atomic rollback and wait for the migration Job, control plane,
   and admin Deployment. The admin image itself performs no migration.
5. Confirm both admin Pods are Ready, the PDB is healthy, both `HTTPRoute`
   parent conditions are Accepted/ResolvedRefs, and the existing inference and
   MCP semantic probes still pass.
6. Run HTTPS browser acceptance for direct routes, session login/logout,
   read-only fleet data, scoped key lifecycle, observability links, and one
   audited task-owned configuration change/rollback. Retain the requested
   console only after all receipts are green.

## Acceptance probes

- Static service: `/healthz` returns `200`; `/admin/` and a direct
  `/admin/models` route return the same SPA entry; immutable assets return
  `200`; `/admin/api/v1/context` returns JSON from the BFF, never HTML.
- Pod boundary: configured UID/GID 101, no service-account token, read-only root
  filesystem, only `/tmp` writable, bounded resources, zero egress, and Envoy-
  only ingress.
- API boundary: unauthenticated admin reads fail; authenticated responses use
  `{meta,data}`; problem responses have `application/problem+json` and a
  server-generated request ID; raw key/session material is absent after its
  one-time response.
- Regression: `/v1`, `/mcp`, `/readyz`, TLS renewal, rate limiting, and existing
  observability scrapes remain healthy.

## Rollback

Use Helm's prior successful revision with atomic wait semantics. Verify the
control-plane Deployment, migration ledger, public inference/MCP probes, and
Gateway conditions after rollback. If only the UI is faulty, set
`adminConsole.httpRoute.enabled=false` and `adminConsole.enabled=false` in the
reviewed release values; do not patch generated objects manually. Database
migrations are additive and are never rolled back by deleting rows or changing
previous migration bytes.

## Deferred hardening

The first local image scan reported no critical findings and two package rows
for one OpenSSL QUIC-server denial-of-service advisory. The static server does
not enable QUIC. Refresh the digest-pinned runtime base and repeat SBOM/scan
custody before retained publication; this does not block source-level Helm and
Gateway acceptance.
