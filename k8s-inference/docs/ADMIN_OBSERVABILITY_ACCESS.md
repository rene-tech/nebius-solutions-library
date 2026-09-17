# Admin observability access

The inference solution uses authenticated Grafana as its only public
observability application. Prometheus, Loki, Tempo, and Alertmanager remain
cluster-private. The admin portal exposes launch actions only after the
component's bounded Prometheus target, health, and data probes pass.

## SAI-22 access boundaries

Preliminary independent review rejected exact commit
`47fe7852b429a2cf7892058b6f96a3652f41bef0` / tree
`c77d413bd0a0e62fdf90b4ea7788f2d401e2ae18` as SOURCE NO-GO. Its control-plane
resource had a signed content digest but no exact chart-owned resource identity
or proof that the current Deployment consumed it; its authored fixture even
accepted the unrelated `fs2-serve-admin-configuration` ConfigMap. This
successor preserves that rejection and the earlier `e72ac70a` replay,
configuration-read, and permit-map findings. It version-bumps the incompatible
owner projection; rejected contracts must never be accepted.

The control plane treats application metrics and logs as different privilege
classes. Global `VIEWER` principals may retain the bounded metrics and
container-status views. Reading `/admin/api/v1/apps/{id}/logs` requires a
global `OPERATOR` or `ADMIN` principal and records the distinct
`app.logs.read` authorization action. A tenant-scoped principal cannot use the
global observability routes.

Loki migration is a serialized three-state protocol, not a one-apply flag flip.
Retained data written while Loki authentication was disabled belongs to Loki's
implicit `fake` tenant. New OTel writes use the distinct `fs2-platform` tenant.
During migration, Grafana and the control plane send the exact bounded
multi-tenant read header `fake|fs2-platform`; Loki enables
the official `querier.multi_tenant_queries_enabled` setting. No caller can add
a third tenant. The legacy
read cohort must remain for at least 168 hours after auth enforcement, matching
the configured retention period and maximum lookback. Removing `fake` requires
a later reviewed source change and evidence that the entire legacy TTL has
elapsed.

The transition order is executable in Terraform and must not be collapsed:

1. apply the Loki ingress policy while `loki_access_phase = "network-bound"`
   keeps Loki auth disabled, then update the OTel gateway to send
   `X-Scope-OrgID: fs2-platform` and apply the bounded readers;
2. while auth is still disabled, collect a **pretransition** marker record.
   Loki necessarily stores that marker under `fake`; the record proves the
   scoped header is configured and both readers can still read the legacy
   cohort. It must not claim that `fs2-platform` was ingested or read;
3. after independent acceptance pins that pretransition envelope, advance
   both phase and rollback floor to `"auth-enforced-validation"`. This is the
   first auth-on apply. The rollback sentinel now forbids any return to an
   auth-off release, while readers continue using `fake|fs2-platform`;
4. only after auth is on can an independent verifier create a
   **posttransition** record proving a new marker was stored under
   `fs2-platform` and both legacy and scoped cohorts were read through Grafana
   and the control plane. A later source successor pins that separate envelope
   before phase and floor advance to `"enforced-dual-read"`.

Each v4 stage envelope binds the exact cluster/run, deployed source
commit/tree, Loki/OTel/control-plane/Grafana Helm revisions, current
Pod-template and image fingerprints, Grafana datasource UID/resourceVersion,
and sealed marker-only proof. It is stored in a digest-named immutable
ConfigMap. An immutable record is historical evidence, not perpetual
authority. Every auth-on plan additionally requires a distinct immutable
release-owner projection whose Ed25519 signature validates against the exact
public trust-root digest pinned in source. The signing private key is never a
Terraform input or Kubernetes workload secret.

The v3 release-owner projection must bind current Helm storage Secret identity and payload
digests for Loki, OTel, Grafana, and the control plane; current Pod templates
and images; the effective Loki configuration; the OTel writer configuration;
the Grafana datasource Secret; the exact chart-owned
`fs2-system/fs2-serve-control-plane-admin-observability` ConfigMap; the two
cached marker objects; the payload inventory; and its admission policy and
binding. Only non-secret identities and SHA-256 digests enter the projection.
Terraform creates an in-place authorization epoch with `timestamp()`, which is
unknown during planning, and makes the projection, trust root, acknowledgement,
workloads, markers, policy, binding, and inventory reads depend on that epoch.
The external verifier therefore runs during apply even for a saved plan. It
uses the exact run-owned kubeconfig and context to reread the signed Loki main
and runtime ConfigMaps, OTel ConfigMap, Grafana datasource Secret, exact
control-plane reader ConfigMap, and payload permit. It also proves that the
current `fs2-system/fs2-serve-control-plane` Deployment mounts that ConfigMap's
sole `config.json` key read-only at the chart path and supplies the exact Loki
URL, bounded `fake|fs2-platform` header, and config-file environment entries to
the `control-plane` container. Secret/configuration bytes remain in the
verifier process; Terraform receives only current metadata and canonical
content digests. Its validity window is at most five minutes, it binds the exact
source-accepted authorization-intent digest, and Terraform compares it to
current deployment objects, current OTel/Grafana `helm_release` revisions,
cached-marker contents, current admission objects, and the fresh apply-time
content projection. A configuration-only change therefore changes the reread
Loki runtime-config, Grafana datasource, or control-plane reader digest even
when no Pod template changes. A replay, signer swap, cached-marker
drift, Secret replacement, expired projection, or caller-selected trust root
fails closed. The source-pinned acknowledgement and trust-root digests remain
`null` in this static candidate; it cannot enable auth.

The old `loki_client_compatibility_receipt` is a SHA-256 of public constants.
It remains only as a deprecated workloads diagnostic, is rejected as a
foundation input, and is not part of any enforcement predicate.

The policy admits port 3100 only from exact run-scoped Grafana and Prometheus
release identities, the OTel gateway, the exact
rendered control-plane identity (`fs2-system`, name and instance
`fs2-serve-control-plane`, component `gateway`), and the Prometheus health
scraper, plus Loki self-traffic. Scientific and model-runtime namespaces are
not peers. Kubernetes Pod labels alone are not an authenticated identity,
however. SAI-03 admission/label custody is currently NO-GO, so this source
pins the accepted custody receipt to `null` and Terraform fails closed before
applying the policy. A reviewed successor must pin the exact independently
accepted SAI-03 receipt in source; a caller cannot self-assert it through
tfvars.

Prometheus's Loki ServiceMonitor currently reaches the shared port 3100. A
NetworkPolicy cannot limit that peer to `/metrics`, so this is an explicit
health-scrape exception rather than a metrics-only boundary. The independently
accepted exception digest is also pinned to `null`; policy application stays
blocked until a later review either accepts the exact exception or replaces it
with metrics-only mediation. Until both dependencies are resolved, this
SAI-22 candidate is not authorized for integration or live use.

Prometheus scrapes the control plane through the dedicated `metrics` service
port (8081), which is served by a same-Pod, fixed-loopback proxy. The
application listener on port 8080 returns 404 for non-loopback `/metrics`
requests. The runtime NetworkPolicy admits model workloads and the public
gateway only to port 8080; only the selected Prometheus Pod may reach port
8081. The proxy mounts no application volumes or credentials and accepts no
caller-selected destination.

The source tree does not log inference request or response bodies in the
first-party control-plane access path. That does not establish the behavior of
every third-party model-runtime image. The v2 payload-safety record must
enumerate five authoritative sets: all current runtime Pods, all current
runtime-producing controllers, all Terraform runtime addresses, all deployable
static runtime manifests, and every catalog runtime binding. Extra discovered
images are permitted; omitting any desired image or protected runtime
namespace is not. Every digest-qualified image carries its consumers, source
sets, and normal, streaming, error, response, and startup synthetic-marker
negative evidence.

The source-pinned inventory digest creates an immutable ConfigMap plus a native
`ValidatingAdmissionPolicy` and binding. The binding covers every enumerated
runtime namespace and denies Pod create/update (including init and ephemeral
containers) when an image is absent from that immutable permit. Admission does
not retroactively validate existing Pods, so the independently signed owner
projection must also bind the exhaustive current Pod/controller enumerations,
the immutable permit, and the exact live policy and binding. Three distinct
digests bind the raw `inventory.json`, the derived `image-<sha256>` key/value
map, and the complete ConfigMap data map. Apply-time verification derives every
permit from `inventory.json`, requires the exact key set with no additions or
omissions, and compares all three digests to both the signed v3 projection and
the source-pinned v4 acknowledgement. Terraform-input
booleans and hashes are preparation data only: they cannot authorize Loki auth
without the external signer, source-pinned public trust root, fresh live-state
projection, and exact artifact custody. Evidence records marker absence only
and must never contain customer payloads or raw runtime logs. The admission
policy constrains image references only. Command, args, environment, `envFrom`,
volume, and mount behavior remain outside that admission expression and require
the independently accepted external SAI-03 custody boundary, which is still
source-pinned to `null` here.

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
# until source-pinned SAI-03 custody and Prometheus-exception dependencies are
# accepted and an independent pretransition record is sealed.
loki_access_phase    = "network-bound"
loki_rollback_floor  = "network-bound"
# loki_migration_acknowledgements = {
#   pretransition  = { ...v4 record and signed v3 owner projection reference... }
#   posttransition = { ...v4 record and signed v3 owner projection reference... }
# }
# loki_identity_custody_receipt            = "<source-pinned SAI-03 receipt>"
# loki_prometheus_health_exception_receipt = "<source-pinned exception receipt>"
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

Static authoring for this correction is an additive successor to independently
rejected commit `a8853e12d59b573d64709761b03e9aff9150cbff` / tree
`b779bc1f5675105d5baf1a2be06b8429f2885782`. That rejection remains the
authoritative negative evidence for replayable freshness, incomplete runtime
enumeration, unsigned payload-safety inputs, and configuration-only drift.
The correction does not claim source GO, integration, deployment, or live
acceptance.

The SAI-22 source candidate authors regressions for all of these contracts, but
the coordinator's static-source boundary for its authoring task prohibited
executing tests, Helm/Terraform commands, scanners, builds, live probes, or
deployment. SAI-03 custody, the Prometheus health exception or its mediated
replacement, exact-image payload-safety inventory, and both stage-specific
migration acknowledgements are also unresolved. A
later authorized integration review must first accept and pin those
dependencies, then execute the checks below from the exact candidate
descendant before any rollout.

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
control-plane port. Confirm that exact run-scoped Grafana, Prometheus, OTel,
and control-plane Pod labels match their Loki ingress peers. Before auth
enforcement, use only a non-sensitive random marker to prove the header-capable
writer and legacy reads; because auth is off, require storage under `fake` and
reject any scoped ingestion/read claim. After the first auth-on apply, use a
different marker to prove `fs2-platform` ingestion and both legacy/scoped
reads. Seal the exact target, source/tree, current object fingerprints, release
   revisions, reread Loki/runtime and datasource content digests, admission
   identities, five-source runtime inventory, validity window, and evidence
   digest. Verify the owner signature against the source-pinned public trust
   root. Run every exact-image payload-marker negative test without displaying
   or retaining payloads.

Alertmanager-only rollback is a reviewed Terraform change that restores the
previous application digests and/or sets
`deployment.observability.alertmanager.enabled = false`, then applies
foundation before workloads. The StatefulSet claim remains retained; rollback
must not delete the namespace or PVC.

For an SAI-22 rollout, capture the prior control-plane image digest and Helm
revision first. Before auth enforcement, rollback may return to the exact
auth-off `network-bound` cohort while retaining the policy and scoped writer.
Auth enforcement begins `fs2-platform` data, so the same change must raise
`loki_rollback_floor` to `auth-enforced-validation`. A later independently
accepted scoped-proof change raises phase and floor to `enforced-dual-read`.
After the first auth-on point, rollback may
use only header-capable control-plane/Grafana versions and must retain Loki
auth, `fake|fs2-platform`, and the NetworkPolicy. A pre-migration release is
not a valid rollback target because it would hide the scoped cohort. The
rollback target needs a newly sealed, source-pinned acknowledgement whose
deployment/image/revision/configuration/datasource fingerprints match that
target and a new five-minute owner projection reopens the exact live release;
a stale pretransition or posttransition envelope is rejected. Auth
enforcement creates the state-retained
`terraform_data.loki_enforced_dual_read_floor` sentinel with
`prevent_destroy`; a normal downgrade plan therefore fails. Never bypass that
guard with state removal. Do not remove the policy in isolation or collapse
metrics back onto the workload-reachable listener. Re-run both the
operator-positive and workload-negative checks after rollback.
