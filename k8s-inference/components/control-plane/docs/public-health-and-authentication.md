# Public health and MCP authentication metadata

The public control-plane readiness endpoint is intentionally status-only.
`GET /readyz` returns HTTP `200` with `{"status":"ready"}` or HTTP `503` with
`{"status":"unavailable"}`. The control plane still evaluates PostgreSQL,
canonical route evidence, activation ownership, admission workers and janitor,
and federated circuits before choosing that status. Their identities, counts,
generations, rejection reasons, and circuit detail are not part of the
unauthenticated response. Operators retain the existing authenticated admin
views and internal telemetry for diagnosis. A viewer-or-higher operator session
can request `GET /admin/api/v1/readiness` for the prior component-level
diagnostic representation; anonymous requests to that route fail closed.

The MCP endpoint uses revocable platform personal access tokens (PATs), not
OAuth access tokens. Clients provision a PAT out of band and send it only as
`Authorization: Bearer <PAT>` to `/mcp`. The protected-resource documents at
`/.well-known/oauth-protected-resource` and
`/.well-known/oauth-protected-resource/mcp` therefore publish the resource URL
and header bearer method, but intentionally omit `authorization_servers` and
`scopes_supported`. The service does not publish an OAuth authorization-server
document or offer OAuth authorization, token, registration, or refresh flows.

PAT scope and model grants are still enforced by the existing verifier after
authentication. Metadata minimization does not make an admin bootstrap token
valid for MCP, change PAT issuance or revocation, or alter inference, catalog,
operation, artifact, upload, or scientific-workload authorization.
