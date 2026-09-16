# MindEval workshop add-on

Deploys two CPU services: a single persistent MindEval inference gateway and two
workshop API/UI replicas. The workshop uses the platform PostgreSQL database in
its own `fs2_workshop` schema. The gateway is deliberately a singleton: its fair
RPM/TPM scheduler and SQLite registration store require one process. Its
Deployment always uses `Recreate` and one replica.

Prerequisites in the release namespace:

- Existing platform control plane with extended `/internal/ext-authz` policy
  headers (`x-fs2-scopes`, `x-fs2-models`, `x-fs2-max-concurrency`).
- `mindeval-token-factory` Secret with key `api-key`.
- `fs2-workshop-credentials` Secret with encryption key file `key`.
- `fs2-serve-database` and `fs2-serve-database-migrations` Secrets, each with DSN
  key `url` and TLS CA key `ca.crt`. DSNs use `/tls/ca.crt` in the pod.
- The existing public Gateway and RWO storage class. The default namespace is
  `fs2-system`; the chart does not create it.

The chart never renders credential values or creates new attendee credentials.
Both runtime images should be immutable digests. `workshop.image` is required;
the default gateway digest contains the calibrated Gemma judge and original
MindEval templates. See `components/mindeval-gateway/evidence/20260916` for
calibration methodology and limitations.

```bash
helm lint k8s-inference/charts/addons/mindeval-workshop \
  --set-string workshop.image="$WORKSHOP_IMAGE"
helm template fs2-mindeval-workshop k8s-inference/charts/addons/mindeval-workshop \
  --namespace fs2-system --set-string workshop.image="$WORKSHOP_IMAGE"
```

After review, installation uses the same release name, chart, namespace and
digest value with `helm upgrade --install --wait --wait-for-jobs --atomic`.
The pre-install/pre-upgrade hook runs `fs2-workshop-migrate` with only the
migration DSN/CA. It preserves failed Jobs for diagnosis and removes successful
Jobs. Runtime pods use the ordinary database role. Gateway SQLite storage is
retained on Helm uninstall; deletion of that PVC is a separate explicit action.

One additional HTTPRoute attaches `/workshop` and `/v1/workshop` to the workshop,
and `/v1/mindeval` to the gateway, with 900-second request/backend timeouts.
Existing routes, speech endpoints and the website are unchanged. A narrowly
scoped additive NetworkPolicy allows these two app labels into the existing
control plane on port 8080 for token verification, speech and platform calls.

The inspected deployment's `fs2-data` namespace has no NetworkPolicies. This
chart deliberately adds none there, since adding the first ingress policy could
isolate the existing database. If another deployment already restricts CNPG,
its owner must allow these workshop runtime and migration pod labels before
installing the hook. The services request no GPUs and introduce no quotas.

Validation on `fs2-storage-h100`: Helm lint passed, and the full rendered chart
passed Kubernetes server dry-run (PVC, two Deployments, two Services, migration
Job, HTTPRoute and additive NetworkPolicy). Rendering used a clearly invalid
example workshop digest while the actual image was being built; that validation
did not deploy any resources.
