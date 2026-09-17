# SAI-13 static source candidate

This branch is the control-plane/admin half of the SAI-13 remediation. It is a
static source candidate only. Under the 2026-09-17 coordinator boundary, no
tests, builds, formatters, package managers, Helm rendering, deployment, live
probe, cluster/provider/registry access, or cleanup was performed.

Source parent: `0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697` (current
`origin/main` when implementation began). Landing companion: repository
`rene-tech/nebius-scientific-ai-platform`, commit
`e39ed8d38a5a3cefabceac7d8ef2f161198fe873` (tree
`b473a90104ed40916bc334737a6eb23573cf9a73`) on branch
`agent/fs2-sai-remediation-sai-13-r20260916-landing`.

## Source changes

- The chart-owned public route claims `PathPrefix /mcp`, so `/mcp/` and nested
  MCP requests cannot fall through to the marketing pod.
- FastAPI retains canonical `/mcp` and returns a bounded, no-store 401 JSON
  response for every non-canonical MCP subpath without inspecting or reflecting
  a bearer token.
- Public API/MCP and admin responses receive CSP, HSTS, MIME-sniffing, referrer,
  permissions, and frame-denial headers at their Gateway routes; the admin
  static service retains the same policy when reached directly in-cluster.
- Vite source maps are disabled, a container-build assertion refuses any `.map`
  output, and a unit contract fixes that setting at false.
- The landing companion applies the strict website CSP/header policy, moves the
  human guide to `/mcp-setup/`, and retains a fail-closed website `/mcp/` guard.

## Authored verification (not executed)

- `components/control-plane/tests/test_helm_chart.py` asserts `PathPrefix /mcp`
  and the exact public response-header set.
- `components/control-plane/tests/test_api_mcp.py` asserts `/mcp/` and a nested
  path return 401 JSON and never reflect a synthetic bearer marker.
- `components/admin-console/src/test/viteConfig.test.ts` and the Dockerfile
  assert that public source maps cannot be produced.
- The landing companion adds `tests/unit/security-boundary.test.ts` and updates
  its browser route inventory to `/mcp-setup/`.

## Integration, rollout, and rollback boundary

These two source commits are inseparable for integration and rollout. An
authorized integration worker must independently review both exact objects,
run all focused and subsystem suites, reconcile with the then-current deployed
shared-service source, record previous image digests and Helm revision, and
stage both changes together. Required live evidence remains: browser headers on
`/`, unauthenticated `POST /mcp/` returns 401 JSON, an admin `.js.map` returns
404, and landing/catalog, lead form, PAT/model grants, sync/stream inference,
MCP canonical calls, admin, and operator flows remain healthy.

Rollback is a coordinated revert of both exact commits followed by restoration
of the recorded prior images/Helm revision; partial rollback is forbidden.
Separate branded origins for marketing, API/MCP, and admin remain the structural
follow-up and residual-risk boundary.
