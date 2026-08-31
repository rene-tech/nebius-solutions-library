# Kubernetes inference add-ons

> Import disposition: optional operational package. Nothing in the canonical
> release or Terraform path installs this package automatically; an operator
> must select and run it explicitly after checking the target cluster.

This directory owns only the standard controller substrate on the selected
Nebius Managed Kubernetes cluster. It does not own application namespaces,
Gateways, model routes, GPU operators, node groups, or StorageClasses.

## Compatibility and pins

The live target is Kubernetes `v1.35.6` (workers `v1.35.7`). The pins below were
checked on 2026-08-27 against the official release and installation sources:

| Component | Pin | Rationale |
|---|---:|---|
| [Gateway API](https://github.com/kubernetes-sigs/gateway-api/releases/tag/v1.5.1) | 1.5.1 | Selected Kubernetes 1.35 compatibility wave; standard CRDs only. |
| [Envoy Gateway](https://github.com/envoyproxy/gateway/releases/tag/v1.8.3) | 1.8.3 | Gateway-source-aligned patch on the Kubernetes 1.35-compatible 1.8 line. |
| [cert-manager](https://github.com/cert-manager/cert-manager/releases/tag/v1.21.1) | 1.21.1 | Chart declares Kubernetes `>=1.22`; Gateway API support is enabled. |
| [KEDA](https://github.com/kedacore/keda/releases/tag/v2.20.2) | 2.20.2 | Chart declares Kubernetes `>=1.23`; no persistent ScaledObject is installed here. |
| [Kueue](https://github.com/kubernetes-sigs/kueue/releases/tag/v0.17.8) | 0.17.8 | Maintained Kubernetes 1.35 pin; asynchronous Jobs only. |
| [KServe](https://github.com/kserve/kserve/releases/tag/v0.20.0) | 0.20.0 | [Standard mode](https://kserve.github.io/website/docs/admin-guide/kubernetes-deployment), with Knative, LocalModelCache, and LLM alpha controllers absent. |

`lock.env` pins the downloaded manifest/chart bytes. Direct controller and
helper images are digest-pinned except Envoy Gateway 1.8.3's chart-selected
controller/data-plane helper tags. Resolving and policy-enforcing those
transitive Envoy image digests, signatures, SBOMs, and vulnerability policy is
deferred hardening; it did not block the manager-authorized first retained
install.

Envoy's chart bundles both Gateway API and Envoy extension CRDs. The installer
keeps `crds.enabled=false`, applies the pinned Gateway API standard manifest,
and applies only the chart's `generated/` Envoy extension CRDs. This avoids
split server-side-apply ownership of Gateway API CRDs.

## Install and inspect

```bash
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBE_CONTEXT="$(kubectl --kubeconfig "$KUBECONFIG" config current-context)"

./k8s-inference/addons/scripts/install.sh
./k8s-inference/addons/scripts/status.sh
./k8s-inference/addons/scripts/smoke.sh
./k8s-inference/addons/scripts/rollback.sh
```

`install.sh` refuses a non-1.35 server or a context other than the selected
current context. It uses checksum-verified local chart archives and idempotent
`helm upgrade --install` with rollback-on-failure, readiness waits, and bounded
history. The install order is Gateway API, Envoy Gateway, cert-manager, KEDA,
Kueue, then KServe CRDs and Standard-mode resources.

The owned namespaces are `envoy-gateway-system`, `cert-manager`, `keda`,
`kueue-system`, and `kserve`. Every controller is constrained to the regular
CPU system pool. KServe has ingress creation and Istio virtual hosts disabled;
the gateway release owns application Gateways and routes. Kueue has no durable
ClusterQueue or ResourceFlavor; model lanes own namespaced LocalQueues and use
Kueue only for asynchronous work.

The smoke script creates the disposable `fs2-addon-smoke` namespace and proves:

- Envoy reconciles a Gateway and HTTPRoute into a `ClusterIP` proxy and serves a backend;
- cert-manager issues a self-signed test Certificate;
- KEDA reconciles a CPU ScaledObject into an HPA;
- Kueue admits and completes a non-GPU queued Job; and
- KServe Standard reconciles a custom InferenceService to Ready.

The script deletes the smoke namespace, ClusterQueue, and ResourceFlavor on
exit and fails if the test creates a LoadBalancer. It creates no PVC.

## Rollback boundary

`rollback.sh` prints release histories and exact `helm rollback` commands.
For a first revision, uninstall in reverse dependency order. CRDs must be
retained until all corresponding custom resources are removed; Gateway API
CRDs are independently owned and are never removed by a Helm rollback.

The provider-owned `compute-csi-default-sc` remains the default StorageClass
with reclaim policy `Delete`. This task neither patches nor adopts it and does
not create a platform StorageClass.
