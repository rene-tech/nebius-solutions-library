# Access, session and audit UI

The React console uses the sealed `/admin/api/v1` access contract implemented
by control-plane commit `798d410d0b6b0360fe3b93a1df07746f6f842f75`.
The browser never receives a Kubernetes, database or observability credential.

## Session boundary

1. `GET /admin/api/v1/session` checks the same-origin opaque operator session.
2. An unauthenticated browser renders the sign-in boundary before any fleet,
   principal, key or audit query is mounted.
3. The submitted bootstrap credential is used only in the `Authorization`
   header of `POST /admin/api/v1/session` and is cleared from the controlled
   input before the request completes. It is not written to local storage,
   session storage, a URL or the query cache.
4. The response installs the server-owned `Secure`, `HttpOnly`,
   `SameSite=Strict` cookie. JavaScript retains only the secret-free
   `OperatorSession` projection.
5. Logout calls `DELETE /admin/api/v1/session`. Tenant-scoped query caches are
   cleared only after the server confirms the logout. A failed logout leaves
   the current view intact and offers a retry.

Any authenticated API response with status 401 signals the session boundary,
clears secret-free cached tenant data and returns to sign-in.

## Role behavior

| Role | Read principals, keys and audit | Create/edit/rotate/revoke keys | Create/edit principals |
| --- | --- | --- | --- |
| Viewer | Yes | No | No |
| Operator | Yes | Yes | No |
| Admin | Yes | Yes | Yes |

A tenant-scoped session cannot change the tenant selector. Global operators
may select a bounded tenant. The server remains the authorization authority;
hidden controls are a usability feature, not an enforcement boundary.

## One-time API-key disclosure

Create and rotate responses are intentionally handled without a TanStack Query
mutation cache. The raw key is copied directly into transient component state,
shown in an `aria-modal` dialog, and cleared on close, backdrop dismissal,
Escape, route navigation or page unmount. Background content is inert while
the dialog is open, and keyboard focus is contained in the dialog. Copying to
the OS clipboard occurs only after the operator presses **Copy key**.

Subsequent list, policy, revoke and audit views render only prefix,
fingerprint, lifecycle and accounting metadata. No raw value is added to
browser storage, URL/query parameters, console logging, telemetry or fixtures
used in the production bundle.

## Data semantics

- Durable API-key usage includes terminal operations, estimated GPU-seconds,
  runtime-reported input/output tokens and modality units.
- Missing runtime measurements remain an em dash with their unavailable
  reason; they are not converted to numeric zero.
- Budgets display used and reserved values separately, plus max concurrency
  and the active rate-window counter.
- The audit page renders append-only actor/action/target/outcome/detail fields.
  Runtime inference requests remain on Operations.

## Verification

Run from `k8s-inference/components/admin-console`:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm run test:run
npm run build
./run_checks.sh
```

The focused tests cover login/logout, 401 handling, viewer RBAC, loading/empty/
error boundaries, partial adapter failure, policy route shapes, one-time copy,
focus containment, and removal of disclosure material from the DOM after
dismissal and navigation. Browser receipts are recorded under
`output/playwright/access-r20260830/`.
