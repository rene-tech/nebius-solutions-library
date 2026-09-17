# Admin observability access

> **SAI-26 source correction:** examples below that set
> `publish_external = true` are preserved rejected-history fixtures and are no
> longer valid inputs. Use the additive admin-session successor described in
> [SAI-26 Grafana admin-session remediation](SAI_26_GRAFANA_ADMIN_SESSION_REMEDIATION.md).

The inference solution uses authenticated Grafana as its only public
observability application. Prometheus, Loki, Tempo, and Alertmanager remain
cluster-private. The admin portal exposes launch actions only after the
component's bounded Prometheus target, health, and data probes pass.

## Terraform configuration

Alertmanager is controlled only from the customer `terraform.tfvars`:

```hcl
deployment = {
  # ...target, pools, models, and application images...
  edge = {
    mode             = "public"
    source_cidrs     = ["192.0.2.0/24"]
    acme_email       = "operator@example.com"
    acme_environment = "production"
  }
  observability = {
    grafana = { publish_external = true }
    alertmanager = {
      enabled   = true
      retention = "120h"
      storage = {
        storage_class_name = "compute-csi-default-sc"
        size_gib           = 10
      }
    }
  }
}
```

`retention` is Alertmanager's data-retention duration. The generated
Alertmanager StatefulSet uses one `ReadWriteOnce` claim and sets both
`whenDeleted` and `whenScaled` to `Retain`. The pinned kube-prometheus-stack
chart adds the corresponding v2 Alertmanager target to Prometheus. The default
receiver is intentionally local and sends no notifications outside the
cluster; notification destinations are a separate operator configuration.

When enabled, the pinned kube-prometheus-stack chart provisions its built-in
Alertmanager datasource with the stable UID `alertmanager` and
`implementation: prometheus`. Foundation state reuses that identity rather than
creating a second sidecar ConfigMap with the same `datasource.yaml` filename.
That gives operators supported alert, silence, contact-point, and
notification-policy views through Grafana's native login. The admin launch
opens Grafana's Silences surface with the chart-owned Alertmanager identity
selected. The Prometheus implementation permits silence management while
contact points and notification policy remain read-only in Grafana, as
documented by
[Grafana's Alertmanager datasource guide](https://grafana.com/docs/grafana/latest/datasources/alertmanager/).

Tempo already has a provisioned, run-qualified datasource. The admin API emits
Grafana's documented Explore `panes` URL with that exact datasource UID and the
selected time range. When a model or operation is selected, the pane includes a
TraceQL filter over the emitted `fs2.model.id` and `fs2.operation.id` span
attributes. Tempo is not represented as having a standalone UI. See [Grafana
Explore URL structure](https://grafana.com/docs/grafana/latest/visualizations/explore/get-started-with-explore/#generate-explore-urls-from-external-tools)
and [TraceQL quoted attribute syntax](https://grafana.com/docs/tempo/latest/traceql/construct-traceql-queries/#quoted-attribute-names).

## Outputs

After the workloads stage applies, Terraform exposes:

- `grafana_url`: authenticated Grafana root;
- `alertmanager_url`: Grafana Silences with this deployment's Alertmanager
  selected, or `null` when disabled;
- `tempo_explore_url`: Grafana Explore with the provisioned Tempo datasource
  selected and a one-hour default range;
- `admin_observability_links`: the non-secret route contract.

The sensitive `access_bundle` repeats all three URLs under `endpoints`, beside
the existing admin, MCP, and inference endpoints and their credentials. The
contextual Tempo URL is returned by `GET /admin/api/v1/observability`; it is
not a second public backend.

## Verification and rollback

Before apply, run Terraform formatting/validation, the deployment-contract and
observability tests, and Helm lint/template for the control-plane chart. On the
target cluster verify all of the following without port forwarding:

1. the Alertmanager StatefulSet and PVC are ready and the PVC uses the selected
   class and size;
2. Prometheus reports the Alertmanager and Tempo targets healthy and their
   build-info series present;
3. Grafana lists both provisioned datasources, and
   `/api/datasources/proxy/uid/alertmanager/api/v2/status` returns Alertmanager's
   v2 status document. Do not use `/api/datasources/uid/alertmanager/health` for
   this check: the built-in Alertmanager datasource is frontend-only, so that
   generic backend-plugin health route returns `Plugin unavailable` even when
   the supported proxy is healthy;
4. the admin Observability page enables Alertmanager and Tempo launch actions;
5. Alertmanager opens Grafana Alerting and Tempo opens Explore with the exact
   Tempo datasource selected;
6. existing Grafana, Prometheus, Loki, OTel, DCGM, Kueue, and KEDA cards remain
   healthy.

Rollback is a reviewed Terraform change that restores the previous application
digests and/or sets `deployment.observability.alertmanager.enabled = false`,
then applies foundation before workloads. The StatefulSet claim remains
retained; rollback must not delete the namespace or PVC.

## Grafana edge authorization

External Grafana publication is fail closed. The abbreviated Grafana setting in
the earlier Alertmanager example is not sufficient for an apply: operators must
provide a dedicated allow-list that is independent of the broader public edge
CIDRs. The allow-list accepts one to eight IPv4 networks, rejects networks
broader than `/8`, and rejects `0.0.0.0/0`.

```hcl
deployment = {
  # ...target, pools, models, applications, and public edge settings...
  observability = {
    grafana = {
      publish_external     = true
      allowed_source_cidrs = ["192.0.2.0/24"]
    }
  }
}
```

The workloads stage attaches two Envoy Gateway v1.8 policies directly to the
`fs2-system/fs2-admin-grafana` HTTPRoute:

- `SecurityPolicy/fs2-admin-grafana-client-cidrs` denies by default and allows
  only the configured operator CIDRs;
- `BackendTrafficPolicy/fs2-admin-grafana-edge-limit` applies the same bounded
  200-request/second local edge limit used by the platform routes.

The policies do not change Grafana authentication, dashboards, datasource
access, or the private Prometheus, Loki, Tempo, and Alertmanager services.
Before promotion, inspect both policy status conditions and prove from an
address outside the allow-list that
`/admin/observability/grafana/login` returns `403`. Then prove from an allowed
operator address that native login, dashboards, Explore, and Alerting still
work. A `401` or login form from the outside probe is a failure because it means
the request reached Grafana.

Rollback removes only these two route-targeted policy resources by reverting
the source commit and reapplying the workloads stage. Preserve the Grafana
HTTPRoute, ReferenceGrant, credentials, dashboards, datasources, and all raw
telemetry backends. Record the pre-apply workloads state identity and review the
rollback plan before either action.

Run the denial probe from a network not covered by
`allowed_source_cidrs`; do not send an admin token, Grafana credentials, or a
cookie:

```bash
test "$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  "${GRAFANA_ORIGIN}/admin/observability/grafana/login")" = "403"
```

**Rejected predecessor claim (not true for the current topology):**

Envoy Gateway evaluates the original downstream client address. Do not add an
`X-Forwarded-For` trust rule as a rollout shortcut. If the real outside and
inside probes do not prove the expected addresses and status codes, stop the
rollout and inspect the load-balancer source-IP path before changing policy.

## SAI-26 correction: admin-session gate replaces source-IP authorization

The preceding **Grafana edge authorization** section is preserved as rejected
design history for candidate `f05b61c14411ee54679b717ba05e6a17983131e7`.
Do not deploy it. The Nebius LoadBalancer path uses
`externalTrafficPolicy = "Cluster"`, while the Envoy Gateway
`ClientTrafficPolicy` configures TLS only. Consequently, the route has no
reviewed, spoof-resistant original-client identity on which `clientCIDRs` can
rely. Its 200-request/second per-proxy limit is also not a login brute-force
control.

The additive SAI-26 successor is documented in
[`SAI_26_GRAFANA_ADMIN_SESSION_REMEDIATION.md`](SAI_26_GRAFANA_ADMIN_SESSION_REMEDIATION.md).
It statically rejects `publish_external = true` and replaces that path with a
two-phase `admin_session_publication_phase` contract. The successor authorizes
every Grafana request through the existing opaque, revocable admin-session
cookie and applies a five-request/minute local limit only to Grafana's `/login`
rule as secondary protection. No source IP or forwarded header is trusted.

The successor must first be applied with phase `prepare`, which keeps the
HTTPRoute attached only to a gateway-native direct-403 `HTTPRouteFilter`; it
has no Grafana backend. Phase `attach` reads the live prepared objects and
refuses the backend switch unless the exact route reports current-generation
`Accepted=True` and `ResolvedRefs=True` and both policies report
current-generation `Accepted=True`. A separate post-attachment gate rechecks
those statuses after the Grafana backend rules are selected. Rollback changes
`attach` back to `prepare`; the Kubernetes successor resources use
`prevent_destroy`, so rollback restores the direct-403 quarantine without
deleting Grafana, its telemetry backends, credentials, dashboards, or
datasources. Record the two Terraform-only attachment receipts outside active
state before rollback retires those receipt instances.
