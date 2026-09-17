# SAI-26 Grafana admin-session remediation

## Status and boundary

This is an additive source candidate only. It has not been integrated,
planned, applied, deployed, or live-tested. Under the parent coordinator's
static-only boundary, its authored Terraform, application tests, status-gate
tests, and rollout commands were not executed.

The rejected predecessor is exact commit
`f05b61c14411ee54679b717ba05e6a17983131e7`, tree
`28cfb50b42c04747d6c9374ec40651b77dd55cf6`. Independent review found three
blocking defects:

1. `clientCIDRs` had no authoritative client identity because the public
   Service uses `externalTrafficPolicy = "Cluster"` and no reviewed proxy
   protocol or client-IP-detection contract exists.
2. The policies were ordered after an already attached public route and had no
   acceptance/resolution receipt, so rejection or controller lag failed open.
3. A 200-request/second local limit applied per Envoy replica was not a useful
   Grafana-login brute-force boundary.

Those facts remain negative evidence. This successor does not reinterpret
them as a passing source-IP design.

## Source contract

The legacy `deployment.observability.grafana.publish_external` and direct
foundation `grafana_publication.enabled` inputs are now rejected. The new
input is:

```hcl
deployment = {
  # Existing target, application, storage, model, and public-edge settings.
  observability = {
    grafana = {
      admin_session_publication_phase = "prepare"
    }
  }
}
```

The accepted phases are:

- `disabled`: no successor resources exist. This is for deployments that have
  never enabled the successor; `prevent_destroy` deliberately blocks using it
  to delete an existing successor.
- `prepare`: Grafana receives its public subpath configuration; the
  ReferenceGrant, ext-auth SecurityPolicy, login-only BackendTrafficPolicy,
  direct-403 `HTTPRouteFilter`, and HTTPRoute exist. The route is attached so
  Envoy Gateway can reconcile authoritative status, but both named rules select
  only the gateway-native 403 response and contain no Grafana backend.
- `attach`: allowed only after the live `prepare` objects match the exact
  contract, the route reports current-generation `Accepted=True` and
  `ResolvedRefs=True`, and both policies report current-generation
  `Accepted=True` for the exact Gateway/listener ancestor.

Four Terraform `moved` blocks map the existing Grafana route, ReferenceGrant,
SecurityPolicy, and BackendTrafficPolicy addresses to their successor
addresses. Object names are retained so an authorized reviewed plan can update
the resources in place instead of deleting and recreating them.

## Authorization and abuse controls

The route-targeted Envoy Gateway v1.8 `SecurityPolicy` uses HTTP external
authorization against
`fs2-system/fs2-serve-control-plane:8080/admin/api/v1/grafana-authorization`.
It forwards only the `cookie` header in addition to Envoy's required protocol
headers, sets `failOpen: false`, uses a two-second timeout, and returns 403 on
authorization-service error.

The endpoint validates the existing `__Host-fs2_admin_session` value through
`OperatorSessionService.verify`. That contract already checks the opaque HMAC
digest, expiry, revocation, and whether the operator principal remains enabled.
Missing, malformed, expired, revoked, or disabled-principal sessions return
403 without reflecting cookie material. A valid session returns an empty 204
with `Cache-Control: no-store`. Grafana native authentication remains enabled
behind this first gate, preserving current operator functionality.

The BackendTrafficPolicy targets only the named `grafana-login` HTTPRoute rule
for exact path `/admin/observability/grafana/login` and permits five requests
per minute per Envoy replica. It is defense in depth; the admin-session ext-auth
boundary is the primary authorization control. The design neither asserts nor
uses client identity derived from the load balancer, `X-Forwarded-For`, custom
headers, or proxy protocol.

Prometheus, Loki, Tempo, Alertmanager, and the PostgreSQL reporting datasource
remain private behind Grafana. The change does not alter model routes, GPU
runtimes, media paths, caches, request-debugging behavior, telemetry storage,
or datasource configuration. Existing NetworkPolicy already permits the
public Envoy pods to reach the same control-plane Service used by the platform
routes; no new broad ingress is added.

## Deny-before-publication receipts

Attachment is deliberately a second apply:

1. Apply `prepare`. Both route rules are updated to reference only
   `HTTPRouteFilter/fs2-admin-grafana-prepare-deny`, whose exact response is
   403; neither rule has a backend. Wait for the route and policies to
   reconcile.
2. Change only the phase to `attach` and generate a fresh reviewed plan. Five
   read-only `kubernetes_resource` data sources bind the plan to the live route,
   direct-response filter, ReferenceGrant, SecurityPolicy, and
   BackendTrafficPolicy. The pre-attach receipt requires exact specs, route
   `Accepted=True` and `ResolvedRefs=True`, and policy `Accepted=True` for the
   exact Gateway/listener ancestor. A
   first-time direct `attach` cannot read the complete prepared contract and
   fails.
3. After Terraform replaces only the quarantined rules with the reviewed
   Grafana backend rules, the read-only
   `wait-for-grafana-admin-session-publication.py` gate issues only `kubectl
   get` calls. It binds the kubeconfig context, cluster ID, and kube-system UID,
   rechecks the exact route, filter, ReferenceGrant, and policy specs, and waits
   for the attached route and both policies to report their required
   current-generation conditions. Terraform records success in the attachment
   receipt.

An already attached, accepted route may be planned again only when the live
specs and all current-generation statuses still satisfy the same gate. Drift
to the rejected CIDR policy, a stale condition, a missing reference, or a
different parent fails the receipt.

## Authorized future verification

No command in this section was run for this source candidate. A future
integration owner must first reconcile the candidate with the currently
deployed shared-service commit and record the prior control-plane image digest
and workloads state identity. The reviewed sequence is:

1. run the focused control-plane, wrapper, Terraform, Helm, and static contract
   suites from an isolated integration branch;
2. review the migration plan for four in-place state moves, one additive
   direct-response filter, zero deletes, and phase `prepare`; apply it and
   prove both route rules resolve only to the exact direct-403 filter;
3. wait for exact policy status, change only the phase to `attach`, review a
   second zero-delete plan, and apply;
4. from an unauthenticated external client, request
   `/admin/observability/grafana/login` with no admin token, Grafana credential,
   or cookie and require 403;
5. establish an admin session through the existing operator workflow, then
   verify Grafana native login, dashboards, Explore, Alerting, Prometheus,
   Loki, Tempo, DCGM, Kueue, KEDA, and the PostgreSQL reporting datasource;
6. smoke-test landing/catalog, PAT and model authorization, synchronous and
   streaming inference, MCP, admin, operations/results/artifacts/uploads,
   queue/model admission, storage, and request-debug enable/capture/view/export/
   purge/disable behavior as required by the parent program.

Do not treat a 401, Grafana login form, connection failure, or 5xx as the
negative test's expected 403. Do not bypass the session gate with an allow-list
or forwarded-header trust rule.

## Rollback

Rollback changes phase `attach` to `prepare` and applies the workloads stage.
That replaces the Grafana backend rules with the direct-403 quarantine while
retaining the route, filter, grant, policies, and their Kubernetes state.
Export the two Terraform-only attachment receipts to the rollout evidence
packet first: returning to `prepare` retires those two receipt instances from
active Terraform state. Review the rollback plan for zero Kubernetes object
deletes before execution. Do not set an established successor to `disabled`:
`prevent_destroy` is intentional and requires an explicit future source review
before any Kubernetes resource removal.
