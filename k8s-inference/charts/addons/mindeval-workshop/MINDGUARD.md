# Optional public MindGuard classifiers

The chart can deploy the exact public MindGuard-4B and MindGuard-8B classifiers
tested in `models/mindguard/QUALIFICATION.md`. They remain observational safety
classifiers, not private MindGuard v2, clinicians, judges or automatic enforcement.
This optional packaging was linted/rendered locally; it has not been deployed.

The chart defaults to **disabled** and therefore changes nothing about an existing
CPU-only workshop release. `values-mindguard.yaml` supplies defaults without
modifying the workshop's ordinary `values.yaml`. Enable either or both public
models explicitly. Each replica requests one GPU by default; no autoscaling, quota,
node, storage-class, namespace, Secret or additional network-policy resources are
created. A new retained cache PVC is optional; an existing claim is mounted only.

## Configuration

The model namespace must already exist and contain the approved HF Secret and
any existing cache or image-pull Secrets. The tested default is regular L40S,
BF16, tensor parallel one, eager execution and native 32768-token context. The
init container downloads the exact checkpoint revision; serving is offline and
does not receive the HF credential. Pod startup/readiness probes and resource
settings match the tested preview renderer. `Recreate` avoids surge GPUs, but
upgrades can interrupt the managed endpoint. Scale-out is configurable, not a
claim that multiple replicas have been performance-qualified.

```yaml
# customer-mindguard.yaml (non-secret references only)
mindguard:
  enabled: true
  namespace: fs2-models
  namePrefix: workshop-models
  existingHuggingFaceSecret: approved-mindguard-hf
  huggingFaceSecretKey: token
  cache:
    existingClaim: approved-mindguard-rwx-cache
  nodeSelector:
    accelerator.fs2.nebius/class: nvidia-l40s-48gb
    capacity.fs2.nebius/type: regular
  models:
    mindguard-4b: {enabled: true, replicas: 1}
    mindguard-8b: {enabled: true, replicas: 1}
```

`models.<id>` may also override `nodeSelector`, `tolerations`, `affinity` and
`resources`. Per-model maps merge with the shared configuration. The selector is
placement configuration, not a request to create or relabel nodes. Setting
`replicas: 0` retains a managed Service/Deployment while creating no model pod.
For 8B-only, explicitly disable the default 4B entry.

Without `cache.existingClaim`, configure `storageClass`, `accessModes`, `size`
and `retain` under `cache`. The default 96Gi RWX claim fits both pinned checkpoints
and is retained on uninstall. Multiple pods on different nodes need RWX storage;
an existing RWO claim is not made multi-node-capable by this chart. Existing cache
contents must be writable by UID/GID 1000 and use the preview's layout:
`/models/<model-id>/<revision>/`. An empty cache requires authorized HF access
and outbound HF connectivity during hydration. Operator-provided networking
must allow CP-to-model port 8000 and downloader egress. Default `fs2-models` and
the preserved model-runtime labels match the current CP egress policy; a custom
model namespace may require a separately coordinated existing-policy update.

The packaged model lock is checked against `models/mindguard/public-models.lock.json`
in tests. Repositories/revisions are intentionally not arbitrary overrides:
the CP response contract embeds those exact revisions. A new checkpoint requires
a synchronized lock/API change and fresh qualification. `mindguard.image` may
override the runtime using an immutable digest; that candidate is not covered by
the current GPU evidence. These options cannot turn a base Qwen model or the
public classifiers into the missing private clinician.

## Render with Helm or pass through Terraform

```bash
helm lint k8s-inference/charts/addons/mindeval-workshop \
  --set-string workshop.image="$WORKSHOP_IMAGE" -f customer-mindguard.yaml
helm template fs2-mindeval-workshop k8s-inference/charts/addons/mindeval-workshop \
  --namespace fs2-system --set-string workshop.image="$WORKSHOP_IMAGE" \
  -f customer-mindguard.yaml
```

The existing Terraform module already forwards `values`; no provider, state or
module changes are necessary. Add to the existing module call (preserve its
other values and pinned workshop/gateway settings):

```hcl
values = [file("${path.module}/customer-mindguard.yaml")]
```

The Terraform/Helm principal must have permission to manage these named resources
in the existing model namespace. Creating both one-replica deployments requests
two GPUs; review available capacity and render/plan before any later apply.
Allow sufficient release timeout for first image pull and weight hydration.

## Current previews and managed adoption

The live workshop continues using explicitly configured CP endpoints:

- `http://fs2-mindguard-r20260916-4b.fs2-models.svc.cluster.local:8000/v1`
- `http://fs2-mindguard-r20260916-8b.fs2-models.svc.cluster.local:8000/v1`

This template neither adopts those existing task-owned resources nor changes
the shared control-plane endpoint settings. They remain the serving endpoints
until a separate coordinated adoption. With the example prefix, new endpoints
would be `http://workshop-models-mindguard-4b.fs2-models.svc.cluster.local:8000/v1`
and the corresponding `-8b`. A no-surprise adoption sequence is:

1. Review existing GPU headroom and named cache/Secret references. Render and
   review the optional resources; do not name them after the live previews.
2. Separately authorize/apply the managed candidate. Test exact model identity,
   health, complete transcript coverage and CP-to-service reachability.
3. Coordinate a CP rollout changing only `FS2_MINDGUARD_4B_ENDPOINT` and/or
   `FS2_MINDGUARD_8B_ENDPOINT` to tested managed service URLs. This chart does not
   own those shared settings. Verify the authenticated public observer path.
4. Keep the old URLs available for rollback. Scale down only the named old
   previews after acceptance; preserve their cache until retention is decided.

Do not apply the full workshop release merely to extract model manifests without
reviewing its other components. This packaging adds no clinical-quality evidence:
the Sword testset is still separately gated and private MindGuard v2 remains
unavailable.

Local validation on 2026-09-16 used Helm 4.3.0: lint passed with classifiers
disabled and with both enabled. Nine render/lock/preview-parity tests pass,
including existing cache, custom storage/image/resources, model selection,
replicas and isolated per-model placement. Combined classifier tests: 58 passed.
