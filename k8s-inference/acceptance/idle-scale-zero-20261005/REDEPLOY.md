# Ownership and redeployment receipt

## What owns the live settings

The supported admin preview/apply API writes durable PostgreSQL model-deployment
revisions. These revisions, not Terraform model replica settings, own availability.
Nineteen pre-existing revisions changed only `availability.minReplicas` from 1 to
0. Eight former static Apps now have minimum 0, maximum 1 and the original
300-second cooldown. Publication, caches and exact runtimes remain available.
All 31 ModelDeployments had minimum zero after adoption on 2026-10-05.

The existing model controller projects those revisions and owns the generated
Deployment/Service/ScaledObject resources. KEDA owns the scale subresource; the
renderer deliberately omits `Deployment.spec.replicas`. A normal controller/API
restart does not reset a floor. The Terraform bootstrap in
`stages/workloads/model_controller.tf` preserves an existing matching admin
identity/revision instead of reapplying its bootstrap settings.

Do not apply legacy static YAML or the MindGuard preview renderer over an adopted
App. Their source defaults are now zero, but that is not a replacement for the
live ownership contract. Optional MindEval Helm classifier packaging stays
disabled on this cluster; its standalone defaults are also zero and it does not
have an independent on-demand scaler. Admin availability remains the supported
way to keep an App hot.

## Retained, reproducible configuration

- `managed-runtime-adoptions.json` retains the **exact eight runtime bundle
  bodies and qualification rows** deployed here, with original immutable image,
  command, Secret references, Service names and PVC references. It contains no
  credentials. Its historical ACE source contains `replicas: 1`; the managed
  renderer omits that field and uses the durable zero-floor policy. The current
  legacy source is now cold as well. Do not rewrite an immutable digest to hide
  that source history.
- `retained_adoptions.py` merges that file into a reviewed base registration,
  preserves every sibling model/pool and rejects a conflicting newer registration.
  It validates bundle content digests and infrastructure shape. It does not apply
  resources or create a second lifecycle owner.
- The canonical source templates remain under `models/visual-science/k8s/`,
  `models/general-media/k8s/` and `models/mindguard/render_preview.py`. Exact
  catalog contracts are under `catalog/runtime/deployment-runtimes/` and
  `catalog/runtime/native/`. Source defaults do not overwrite an existing desired
  revision.
- `idle-adoption.values.yaml` records the scoped Helm reference merge fragment.
  It is **not** sufficient as a complete release values file: preserve current
  API/tools/workshop images, execution/scheduling maps and all sibling settings.
- The existing H200 group declaration and portable Terraform merge fragment are
  `models/general-media/wan2-h200-existing-pool.json` and the adjacent
  `wan2-h200-pool.tfvars.example`. No capacity was created. Existing Kueue flavor
  quotas were preserved and only the declared two-node pool was appended.
- `retire_static_routes.py` reproducibly removes the adopted Apps' redundant
  legacy overrides from the current lean-route base, retaining selected runtime
  records and sibling routes. Its live result is
  `fs2-idle-routes-1a1ea92b8529`.

Offline reconstruction against the saved pre-MindGuard base was run and returned
the exact live names:

```
fs2-idle-envelope-21ef2f1eac8f
fs2-idle-bundles-37bb372fe37d
```

Use raw `infrastructure-envelope.json` and `renderer-bundles.json` from the
reviewed base release as inputs:

```bash
PYTHONPATH=k8s-inference/components/control-plane/src \
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/idle-scale-zero-20261005/retained_adoptions.py \
  --envelope /PRIVATE/base/infrastructure-envelope.json \
  --bundles /PRIVATE/base/renderer-bundles.json
```

The command prints candidate immutable ConfigMaps; it does not deploy them.
The retained registrations declare no GPU-snapshot qualification and do not
invent a benchmark-derived fast-start level. Live single-request evidence is in
the adjacent README and private receipts, with untested bounds named explicitly.

## Remaining root Terraform reconciliation — not claimed complete

The existing root retained-profile qualification join does **not** yet include
all eight adopted Apps. A wholesale workloads apply from the old pinned release
overlay can replace the current envelope/bundles/routes and discard these
registrations. The installed Helm release history also predates several
image-only successor deployments. Do not mistake `helm get values` from that
older revision for the current complete live Pod template.

Before the next whole-stack Terraform/Helm deployment, the release owner must
reconcile the current source-backed registration extension, H200 scheduling
contract, lean routes and latest immutable API/tools/workshop images into its
authoritative release overlay. Keep `workload_owner = "controller"`, preserve
the existing handoff/ownership receipt and never reintroduce static writers for
these eight Apps. Review a non-mutating plan/render for removal of registrations,
duplicate owners and restored hot floors. Then verify public wake/result/drain
on the exact resulting release. This task performed **no Terraform apply or
state migration** and does not claim that this remaining whole-stack
reconciliation has been completed.

## Verification and rollback

The scoped test suite renders all eight Apps with one Deployment/ScaledObject
owner, no controller-written replica field, minimum 0 and maximum 1. It verifies
source default zero, exact retained digest reconstruction, idempotent merge and
preservation of siblings. The optional MindGuard Helm chart renders no classifier
resources by default and cold resources when explicitly enabled without a floor.

Private backups, prior UIDs/resourceVersions, admin revisions, public operations
and rollout observations are under
`/home/tux/secure-handoff/fs2-idle-scale-zero-20261005/`. A floor rollback is a
new supported admin revision, not an old Deployment applied over the controller.
No PVC, cached weights, customer identity or active Lynx operation was deleted.
