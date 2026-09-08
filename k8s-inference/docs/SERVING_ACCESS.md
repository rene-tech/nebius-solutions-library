# Shared platform serving access

The operator owns and deploys Apps. Customers consume those existing deployments
using API keys whose `models` grant includes the App's public model ID (or `*`).
An App does not require a deployment copy or an owner-tenant key per customer.
OpenAI HTTP, native HTTP and MCP use this same model-grant boundary, together
with their existing functional scopes such as `inference.invoke`, `mcp.invoke`
and `catalog.read`. Disabled inference owners and explicit user App restrictions
still constrain their keys; a restriction never expands a key's model grant.

Customer tenant, inference owner and key identities remain attached to every
operation, encrypted request/result, idempotency namespace and usage record.
Two customers can invoke one exact deployment revision without sharing results
or billing identity. Even identical principal names and idempotency keys in two
tenants remain distinct. A customer's lifecycle/result request for another
tenant's operation returns not found.

## Compatibility with existing deployments

- Existing `policy.visibility: Tenant` with no `allowedPrincipalIds` is the
  legacy platform catalog setting. It no longer compares a caller's tenant to
  the operator's `tenantId`; the API-key model grant controls customer access.
  No manifest/schema rewrite or additional access-mode flag is required.
- Explicit `Private` visibility or a nonempty `allowedPrincipalIds` list remains
  restricted. These legacy principal IDs are interpreted within the deployment
  owner's tenant. Access requires **both** that tenant and an exact listed
  principal, plus the normal model grant. Matching a principal name in another
  tenant does not grant access. An empty Private allowlist admits nobody.
  The release owner inspected all 16 live managed serving deployments on
  September 8, 2026: all use Tenant visibility and empty principal lists, so
  this compatibility exception affects none of the current Apps.
- Research-only, license and commercial-use metadata remains visible and is not
  a separate customer scope requirement. Legacy `use.nonclinical` and
  `use.noncommercial` scope values remain accepted for token compatibility,
  but do not grant extra model access. Operator runtime/license qualification,
  immutable artifacts, publication validity and snapshot compatibility remain
  unchanged.

The durable admission fence still checks the exact deployment namespace/name,
current revision digest, public model identity and enabled desired state. Only
the incorrect comparison of deployment owner to consuming customer was removed.
No stale or disabled deployment is made routable by this change.

## Regression coverage

`components/control-plane/tests/test_platform_serving_access.py` exercises
actual admission and worker dispatch for two non-owner customer tenants through
OpenAI HTTP, native HTTP and MCP. It verifies same-revision dispatch, exact-ID
replay, separate per-owner usage and result isolation, denied model keys,
missing functional scopes, disabled owners and tenant-qualified private rules.
PostgreSQL-marked variants repeat the shared dispatch/usage/result checks against
the real migrated store, not an invented SQL projection.

These tests use synthetic payloads and a local stub runtime, not live model
benchmarks. Live public acceptance and release receipts are recorded separately
by the release owner.

Implementation verification on September 8, 2026: all 14 new cases passed
(12 local cases and two real PostgreSQL cases); 72 existing registry, dynamic
route, admission and API/MCP cases passed. The existing PostgreSQL clone test
also passed its stale-revision and disabled-deployment fence assertions. Ruff,
format, diff checks and mypy on the five changed serving source files passed.
The PostgreSQL tests used one owned loopback-only, tmpfs-backed disposable
container, not the platform database; it was stopped and automatically removed
after confirming no remaining test connections. Pre-existing pytest cleanup
warnings for unrelated old temporary directories were not modified or hidden.
