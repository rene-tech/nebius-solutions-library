<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# ModelExpress Helm Chart

This Helm chart deploys ModelExpress, a model serving and management platform, to Kubernetes. For the broader deployment guide covering Docker, standalone K8s, and P2P transfers, see [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Access to NVIDIA Container Registry (nvcr.io) for pulling the ModelExpress image

## Installation

### 1. Obtain short-lived workload pull authorization

Long-lived NVCR API keys, manual Docker login, and hand-created registry
Secrets are not supported. The reviewed workload-identity broker exchanges a
projected service-account token for a pull-only credential scoped to one exact
repository and manifest digest. `deploy.sh` validates the broker's signed
receipt and rotates the following Secret without printing its data:

```yaml
# values.yaml (default)
imagePullSecrets:
  - name: fs2-modelexpress-pull
```

The trust policy deliberately contains null broker identities until Platform
Security approves the production issuer, service account, registry and broker.
The source candidate therefore fails closed; do not substitute a static key.

### 2. Install through the signed release gate

```bash
export FS2_SAI24_RELEASE_CLOSURE=/absolute/path/to/release-image-closure.json
export FS2_IMAGE_GATE_TOOLCHAIN=/absolute/path/to/execution-toolchain.lock.json
export FS2_IMAGE_GATE_BOOTSTRAP=/external/root-owned/path/to/fs2-capsule-bootstrap
export FS2_EXTERNAL_CAPSULE_TRUST=/external/root-owned/path/to/capsule-trust.json
export FS2_WORKLOAD_OIDC_TOKEN_FILE=/absolute/path/to/projected/token
export FS2_WORKLOAD_AUTH_IDENTITY=/absolute/path/to/workload-identity.json
export FS2_WORKLOAD_AUTH_RUN_ROOT=/absolute/path/to/private-run-directory
export FS2_MODELEXPRESS_IMAGE='nvcr.io/approved/repository@sha256:<64-hex-digest>'
"$FS2_IMAGE_GATE_BOOTSTRAP" shell-entry \
  --external-trust "$FS2_EXTERNAL_CAPSULE_TRUST" \
  --toolchain "$FS2_IMAGE_GATE_TOOLCHAIN" \
  --source-root /absolute/read-only/candidate/k8s-inference \
  --entry charts/addons/modelexpress/deploy.sh --
"$FS2_IMAGE_GATE_BOOTSTRAP" shell-entry \
  --external-trust "$FS2_EXTERNAL_CAPSULE_TRUST" \
  --toolchain "$FS2_IMAGE_GATE_TOOLCHAIN" \
  --source-root /absolute/read-only/candidate/k8s-inference \
  --entry charts/addons/modelexpress/deploy.sh -- -f values.yaml
```

The adjacent detached signature is mandatory. The helper verifies the signed
source, release name, namespace, chart tree, selected mode and values. The
post-renderer compares the exact manifest emitted by the actual install or
upgrade invocation with the retained render, and validates the short-lived
pull receipt against every private digest in that same manifest. Direct Helm
release commands are not a supported installation path.

## Configuration

### ⚠️ Important: Override Production Values

**CRITICAL:** The `values-production.yaml` file contains example values that **MUST** be overridden for your environment:

- **Domain Names**: `modelexpress.yourdomain.com` is a placeholder - replace with your actual domain
- **TLS Certificates**: The TLS configuration references `modelexpress-tls` secret - ensure this exists or update the configuration
- **Storage Classes**: `fast-ssd` storage class may not exist in your cluster - verify or change to an available storage class
- **Node Selectors**: `node-type: "compute"` and tolerations may not match your cluster setup

**Always review and customize production values before deployment:**

```bash
# Copy and customize production values
cp helm/values-production.yaml helm/my-production-values.yaml
# Edit my-production-values.yaml with your actual values
"$FS2_IMAGE_GATE_BOOTSTRAP" shell-entry \
  --external-trust "$FS2_EXTERNAL_CAPSULE_TRUST" \
  --toolchain "$FS2_IMAGE_GATE_TOOLCHAIN" \
  --source-root /absolute/read-only/candidate/k8s-inference \
  --entry charts/addons/modelexpress/deploy.sh -- -f my-production-values.yaml
```

The following table lists the configurable parameters of the ModelExpress chart and their default values.

| Parameter                                    | Description                                    | Default |
|----------------------------------------------|------------------------------------------------|---------|
| `replicaCount`                               | Number of ModelExpress replicas                | `1`     |
| `image.repository`                           | ModelExpress image repository                  | `nvcr.io/nvidia/ai-dynamo/modelexpress-server` |
| `image.pullPolicy`                           | Image pull policy                              | `IfNotPresent` |
| `image.digest`                               | Protected exact manifest digest (required)     | blocked/empty |
| `imagePullSecrets`                           | Broker-managed short-lived pull Secret         | `fs2-modelexpress-pull` |
| `nameOverride`                               | Override the chart name                        | `""`     |
| `fullnameOverride`                           | Override the full app name                     | `""`     |
| `serviceAccount.create`                      | Create a service account                       | `true`   |
| `serviceAccount.annotations`                 | Service account annotations                    | `{}`     |
| `serviceAccount.name`                        | Service account name                           | `""`     |
| `podAnnotations`                             | Pod annotations                                | `{}`     |
| `podSecurityContext`                         | Pod security context                           | `{}`     |
| `securityContext`                            | Container security context                     | `{}`     |
| `service.type`                               | Service type                                   | `ClusterIP` |
| `service.port`                               | Service port                                   | `8001`   |
| `ingress.enabled`                            | Enable ingress                                 | `false`  |
| `ingress.className`                          | Ingress class name                             | `""`     |
| `ingress.annotations`                        | Ingress annotations                            | `{}`     |
| `ingress.hosts`                              | Ingress hosts                                  | `[]`     |
| `ingress.tls`                                | Ingress TLS configuration                      | `[]`     |
| `resources.limits.cpu`                       | CPU limit                                      | `500m`   |
| `resources.limits.memory`                    | Memory limit                                   | `256Mi`  |
| `resources.requests.cpu`                     | CPU request                                    | `200m`   |
| `resources.requests.memory`                  | Memory request                                 | `128Mi`  |
| `persistence.enabled`                        | Enable persistence                             | `true`   |
| `persistence.storageClass`                   | Storage class                                  | `""`     |
| `persistence.accessMode`                     | Access mode                                    | `ReadWriteOnce` |
| `persistence.size`                           | Storage size                                   | `10Gi`   |
| `persistence.mountPath`                      | Mount path                                     | `/root`  |
| `env.MODEL_EXPRESS_SERVER_PORT`              | Server port                                    | `8001`   |
| `env.MODEL_EXPRESS_LOG_LEVEL`                | Logging level                                  | `info`   |
| `env.MODEL_EXPRESS_CACHE_DIRECTORY`          | Cache directory                                | `/app/cache` |
| `env.MX_METADATA_BACKEND`                    | Distributed backend (`redis` or `kubernetes`). Server fails to start without this. | `<required>` |
| `env.REDIS_URL`                              | Redis connection URL; required when backend is `redis`. Chart does not bundle Redis. | `<required when backend=redis>` |
| `livenessProbe.enabled`                      | Enable liveness probe                          | `true`   |
| `readinessProbe.enabled`                     | Enable readiness probe                         | `true`   |
| `nodeSelector`                               | Node selector                                  | `{}`     |
| `tolerations`                                | Tolerations                                    | `[]`     |
| `affinity`                                   | Affinity rules                                 | `{}`     |

## Examples

### Basic Installation

```bash
Use the capsule `shell-entry` invocation above without `-f`.
```

### Custom Image Repository

```yaml
# values.yaml
image:
  repository: your-registry/modelexpress-server
  digest: sha256:<64-hex-digest-from-protected-inventory>
  pullPolicy: Always
```

### With Ingress

**⚠️ Warning:** Replace `modelexpress.example.com` with your actual domain and ensure the TLS secret exists.

```yaml
# values.yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: modelexpress.example.com  # ← Replace with your actual domain
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: modelexpress-tls  # ← Ensure this secret exists
      hosts:
        - modelexpress.example.com  # ← Replace with your actual domain
```

### With Custom Resources

```yaml
# values.yaml
resources:
  limits:
    cpu: 1000m
    memory: 1Gi
  requests:
    cpu: 500m
    memory: 512Mi
```

### With Custom Storage

```yaml
# values.yaml
persistence:
  enabled: true
  storageClass: fast-ssd
  size: 50Gi
  mountPath: /app/data
```

### With Additional Environment Variables

```yaml
# values.yaml
extraEnv:
  - name: CUSTOM_VAR
    value: "custom_value"
  - name: SECRET_VAR
    valueFrom:
      secretKeyRef:
        name: modelexpress-secrets
        key: secret-key
```

## Upgrading

```bash
"$FS2_IMAGE_GATE_BOOTSTRAP" shell-entry \
  --external-trust "$FS2_EXTERNAL_CAPSULE_TRUST" \
  --toolchain "$FS2_IMAGE_GATE_TOOLCHAIN" \
  --source-root /absolute/read-only/candidate/k8s-inference \
  --entry charts/addons/modelexpress/deploy.sh -- --upgrade
```

## Uninstalling

Removal is intentionally outside this helper and requires the platform's
separately reviewed retention/decommission workflow. Raw Helm mutation is not
an accepted path.

## Troubleshooting

### Check Pod Status

```bash
kubectl get pods -l app.kubernetes.io/name=modelexpress
```

### Check Logs

```bash
kubectl logs -l app.kubernetes.io/name=modelexpress
```

### Check Service

```bash
kubectl get svc -l app.kubernetes.io/name=modelexpress
```

### Port Forward for Local Access

```bash
kubectl port-forward svc/my-modelexpress 8001:8001
```

### Image Pull Issues

If you encounter `ErrImagePull` or `ImagePullBackOff` errors:

1. **Check whether the broker-managed Secret reference exists (never print its data):**
   ```bash
   kubectl get secret fs2-modelexpress-pull -n your-namespace -o name
   ```

2. **Check whether the expected Secret name is referenced:**
   ```yaml
   imagePullSecrets:
     - name: fs2-modelexpress-pull
   ```

3. **Check pod events for bounded diagnostic metadata:**
   ```bash
   kubectl describe pod -l app.kubernetes.io/name=modelexpress -n your-namespace
   ```

## Image identity

The repository default names the upstream ModelExpress repository but leaves
the digest empty. A protected release must supply the independently resolved
and scanned digest plus the matching short-lived pull receipt. Tags are never
accepted as deployment identity.

## Contributing

When contributing to this Helm chart, please ensure:

1. All templates follow Helm best practices
2. Values are properly documented
3. Examples are provided for common use cases
4. Tests are included for the chart
