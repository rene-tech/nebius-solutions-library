# Admin observability access

The inference solution uses authenticated Grafana as its only public
observability application. Prometheus, Loki, Tempo, and Alertmanager remain
cluster-private. The admin portal exposes launch actions only after the
component's bounded Prometheus target, health, and data probes pass.

## SAI-22 access boundaries

The control plane treats application metrics and logs as different privilege
classes. Global `VIEWER` principals may retain the bounded metrics and
container-status views. Reading `/admin/api/v1/apps/{id}/logs` requires a
global `OPERATOR` or `ADMIN` principal and records the distinct
`app.logs.read` authorization action. A tenant-scoped principal cannot use the
global observability routes.

Loki migration is a serialized two-state protocol, not a one-apply flag flip.
Retained data written while Loki authentication was disabled belongs to Loki's
implicit `fake` tenant. New OTel writes use the distinct `fs2-platform` tenant.
During migration, Grafana and the control plane send the exact bounded
multi-tenant read header `fake|fs2-platform`; Loki enables
`multi_tenant_queries_enabled`. No caller can add a third tenant. The legacy
read cohort must remain for at least 168 hours after auth enforcement, matching
the configured retention period and maximum lookback. Removing `fake` requires
a later reviewed source change and evidence that the entire legacy TTL has
elapsed.

The transition order is executable in Terraform and must not be collapsed:

1. apply the Loki ingress policy while `loki_access_phase = "network-bound"`
   keeps Loki auth disabled, then update the OTel gateway to write with
   `X-Scope-OrgID: fs2-platform`;
2. apply workloads so the control-plane and Grafana readers use
   `fake|fs2-platform`, then capture the exact
   `loki_client_compatibility_receipt` output;
3. pass that receipt to foundation, set both `loki_access_phase` and
   `loki_rollback_floor` to `"enforced-dual-read"`, and only then allow the
   Loki release to enable auth.

The policy admits port 3100 only from Grafana, the OTel gateway, the control
plane, and the Prometheus health scraper, plus Loki self-traffic. Scientific
and model-runtime namespaces are not peers. Kubernetes Pod labels alone are
not an authenticated identity, however. SAI-03 admission/label custody is
currently NO-GO, so this source pins the accepted custody receipt to `null` and
Terraform fails closed before applying the policy. A reviewed successor must
pin the exact independently accepted SAI-03 receipt in source; a caller cannot
self-assert it through tfvars. Until that dependency is resolved, this SAI-22
candidate is not authorized for integration or live use.

Prometheus scrapes the control plane through the dedicated `metrics` service
port (8081), which is served by a same-Pod, fixed-loopback proxy. The
application listener on port 8080 returns 404 for non-loopback `/metrics`
requests. The runtime NetworkPolicy admits model workloads and the public
gateway only to port 8080; only the selected Prometheus Pod may reach port
8081. The proxy mounts no application volumes or credentials and accepts no
caller-selected destination.

The source tree does not log inference request or response bodies in the
first-party control-plane access path. That does not establish the behavior of
every third-party model-runtime image. Promotion therefore remains gated on a
private, exact-image review and payload-marker negative test for every runtime
image in the release. The test must record only the marker's absence and must
not copy customer data or raw runtime logs into evidence.

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

# SAI-22 starts in the auth-off preparation phase. These values cannot advance
# until the source-pinned SAI-03 custody dependency is accepted.
loki_access_phase    = "network-bound"
loki_rollback_floor  = "network-bound"
# loki_client_compatibility_receipt = "<exact workloads output after phase 2>"
# loki_identity_custody_receipt     = "<exact source-pinned SAI-03 receipt>"
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

The SAI-22 source candidate authors regressions for all of these contracts, but
the coordinator's static-source boundary for its authoring task prohibited
executing tests, Helm/Terraform commands, scanners, builds, live probes, or
deployment. SAI-03 custody is also still unresolved. A later authorized
integration review must first accept and pin that dependency, then execute the
checks below from the exact candidate descendant before any rollout.

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

For SAI-22 specifically, also verify that a `VIEWER` receives 403 from the app
logs route while an `OPERATOR` still receives the bounded response; the
ServiceMonitor scrapes the named `metrics` port; a model/scientific workload
receives a network denial when connecting to Loki port 3100; Grafana, the OTel
gateway, the control plane, and Prometheus retain their required Loki flows;
and a model workload cannot retrieve tenant-labelled samples from either
control-plane port. Run the exact-image payload-marker negative test without
displaying or retaining payloads.

Alertmanager-only rollback is a reviewed Terraform change that restores the
previous application digests and/or sets
`deployment.observability.alertmanager.enabled = false`, then applies
foundation before workloads. The StatefulSet claim remains retained; rollback
must not delete the namespace or PVC.

For an SAI-22 rollout, capture the prior control-plane image digest and Helm
revision first. Before auth enforcement, rollback may return to the exact
auth-off `network-bound` cohort while retaining the policy and scoped writer.
Auth enforcement begins `fs2-platform` data, so the same change must raise
`loki_rollback_floor` to `enforced-dual-read`. After that point, rollback may
use only header-capable control-plane/Grafana versions and must retain Loki
auth, `fake|fs2-platform`, and the NetworkPolicy. A pre-migration release is
not a valid rollback target because it would hide the scoped cohort. Auth
enforcement creates the state-retained
`terraform_data.loki_enforced_dual_read_floor` sentinel with
`prevent_destroy`; a normal downgrade plan therefore fails. Never bypass that
guard with state removal. Do not remove the policy in isolation or collapse
metrics back onto the workload-reachable listener. Re-run both the
operator-positive and workload-negative checks after rollback.
