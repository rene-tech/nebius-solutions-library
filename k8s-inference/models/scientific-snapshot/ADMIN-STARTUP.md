# Optional GPU snapshots for scientific batch jobs

Normal model loading remains available for every model. A GPU checkpoint is
an optional, runtime-specific acceleration, not a replacement for the model
weights or container image. CPU-only models do not need GPU snapshots; for
models whose native loader takes only a few seconds, restoration may be slower.

The first qualified production adapter is Protenix v2's `sample-structure`
stage. Its [paired H100 measurements](../../acceptance/h100-fleet/snapshots/protenix-v2-h100-20260907.md)
show the exact cache conditions and compatibility; these measurements alone
do not assert that an endpoint has deployed the option.

## Deployment

Configure `deployment.scientific_batch.gpu_snapshots` in `terraform.tfvars`:

- `bundles`: registry entries keyed by their `bundle_id`. Each entry records
  the immutable runtime/tools images, captured source files, model identity,
  shared-cache claim and path, manifest, qualification receipt and GPU/driver
  compatibility. The measured Protenix entry is in
  [protenix-v2-bundle.json](../../acceptance/h100-fleet/snapshots/protenix-v2-bundle.json).
- `cache.claim_name`, `cache.storage_class_name`, `cache.size_gib`: the RWX
  cache used by these bundles. No local NVMe is required.
- `adopt_existing`: set true when transferring a qualification run's existing
  PVC and source ConfigMaps to Terraform ownership. Fresh deployments default
  to false and create these resources. Adoption does not copy or recapture a
  snapshot. A new empty cache must be populated and qualified for its actual
  runtime before a bundle is enabled; otherwise normal loading is the fallback.

Terraform creates/manages the cache and the exact source ConfigMaps. The
snapshot registry supplements the unchanged scientific execution recipe. It
does not alter GPU counts, node-group ceilings, Kueue quotas or normal-load
defaults. Do not reuse an H100 qualification as proof for a different GPU.

## Live selection

In the admin console, open Scientific runs → model policy → Edit policy.
`Startup for <stage>` offers inherited/default loading, explicit normal loading,
and the qualified bundles registered for that model/stage. Policies can be
global or tenant-specific; a tenant's explicit stage choice overrides the
global choice. Existing dispatch pause and active-run cap controls still apply.

The same API is exposed under `/admin/api/v1/scientific-model-policies`.
Policy responses include `startup_options` and `desired.startup_policies`.
For example, a policy update can include:

```json
{
  "expected_revision": 0,
  "paused": false,
  "max_active_runs": null,
  "reason": null,
  "startup_policies": {
    "sample-structure": {
      "backend": "cuda-criu",
      "bundle_id": "protenix-v2-h100-cuda-criu-20260907-r2"
    }
  }
}
```

Use the current displayed revision, not always zero. To force normal loading,
use `backend: "normal-load", bundle_id: null`. An empty `startup_policies`
object resets this scope to inherited/default behavior. Omitting the field
preserves the current choice for compatibility with dispatch-only clients.

The full selected bundle is frozen when a new run is admitted. Changing a
policy cannot change an already accepted run or its retries. The restore
supervisor checks compatibility, uses independent mutable scratch space per
Pod, and falls back to ordinary loading after an incompatible/failed restore.
The original scientific command, result collector and semantic checks remain
in use. API selection is therefore not proof of successful restoration:
verify the actual restore outcome and startup timings of the submitted run.
