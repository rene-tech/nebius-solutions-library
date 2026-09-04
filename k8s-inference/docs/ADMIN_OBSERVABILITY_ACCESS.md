# Admin observability access

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

When enabled, Terraform provisions an Alertmanager datasource with
`implementation: prometheus` in Grafana. That gives operators supported alert,
silence, contact-point, and notification-policy views through Grafana's native
login. The admin launch opens Grafana's Silences surface with the provisioned
Alertmanager identity selected. The Prometheus implementation permits silence
management while contact points and notification policy remain read-only in
Grafana, as documented by
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
3. Grafana lists both provisioned datasources;
4. the admin Observability page enables Alertmanager and Tempo launch actions;
5. Alertmanager opens Grafana Alerting and Tempo opens Explore with the exact
   Tempo datasource selected;
6. existing Grafana, Prometheus, Loki, OTel, DCGM, Kueue, and KEDA cards remain
   healthy.

Rollback is a reviewed Terraform change that restores the previous application
digests and/or sets `deployment.observability.alertmanager.enabled = false`,
then applies foundation before workloads. The StatefulSet claim remains
retained; rollback must not delete the namespace or PVC.
