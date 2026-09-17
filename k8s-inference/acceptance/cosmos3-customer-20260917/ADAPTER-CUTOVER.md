# Scoped Cosmos media adapter cutover — 2026-09-17

This is an operator plan, not a customer-readiness receipt. LeRobot dataset
semantic qualification and public parent/child acceptance remain separate gates.

## Exact unchanged dependencies

- Context `fs2-remediation-sandbox2`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`,
  project `project-e00rene`, region `eu-north1`.
- Runtime image `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/vllm/vllm-omni@sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587`.
- Model revision `7a312c868bcce8e40b3eb40861300a9d0ba3fde1`; model manifest
  `sha256:49b4bf03d84857892b86366ef2f6c3b68ac575cb1a3f2de4122c41e638f4cc6e`.
- Snapshot `cosmos3-nano-h100-cuda-criu-20260907-r7`, bundle manifest
  `4975911cfd2ba92a0ef0537d69966ef63af457eca4f658c796e88851a9500bb8`,
  existing qualification receipt
  `6f0c8c7d53ad7a6612f4469bbd2855e6172f4ff6f749fdadb13d0f71f14cf400`.
- Read-only model PVC `cosmos3-nano-cache-rwx-8d453c7f`; snapshot PVC
  `fs2-fleet-snapshots-rwx-r20260907`, bundle path `cosmos-r7`.

## Minimal changed serving objects

The sidecar starts `python3 /adapter/adapter.py` separately from the snapshotted
`vllm-omni` process. Updating its ConfigMap does not alter checkpoint bytes.

1. Regenerate the model-controller renderer contract from tracked
   `models/general-media/k8s/cosmos3-nano.yaml`; do not patch controller-owned
   serving objects out of band. The old registered template digest is
   `sha256:b2ce3b2351242330d1bb17f701968cc044bc244ff760567faaadbe915bb55214`.
   The old concrete burst deployment spec digest is
   `sha256:f0157868064ea9f9014091b58b40a66bbd084c89fa27c96ab930e48d2e8923a3`.
2. `fs2-models/cosmos3-nano-adapter` changes adapter source SHA-256 from
   `65247912a803546358a337eb1c4024afbc3521a42ebbd4d5bacd3e50057085b2` to
   `8b5c283086fbb00405889d861adcead0cc5461091dc036e7e5af4c668b5fb6dd`.
3. The managed Cosmos template adds a shared 512 MiB disk-backed
   `cosmos-control-tmp` emptyDir at `/cosmos-control-tmp` to both containers,
   and a bounded 2 GiB memory-backed `runtime-shm` emptyDir at `/dev/shm` to the
   runtime. The latter resolves observed default-64-MiB shared-memory exhaustion
   during larger video processing. GPU count, CPU/memory bounds, precision,
   model bytes and snapshot profile stay unchanged.
4. Terraform recalculates the immutable bundle/envelope/bootstrap ConfigMap
   names and template identity. Previous names are
   `fs2-system/fs2-model-bundles-2853d0e63f3e861e`,
   `fs2-system/fs2-model-envelope-874fdf23bd1a3584`, and
   `fs2-system/fs2-model-bootstrap-29a51b1de3c6adbe`. Root must review the exact
   generated plan and new identity; no guessed digest is a release input.

## Verification and rollback

Before shared promotion, use `render_media_preview.py --snapshot-bundle` with
the live r7 bundle and `fallback=fail`; validate actual restore, V2V/transfer
outputs, checksums and decoded frames. The preview is pinned to reserved idle
H100 capacity, uses dedicated selectors and a
read-only cache verifier. It cannot be adopted by the model controller or shared
Service. Only `fs2-models/cosmos3-nano-media-preview-r20260917` Deployment and
same-named immutable ConfigMap are task-owned. Earlier normal-load outputs are
mechanics evidence only and cannot stand in for snapshot compatibility.

After root-approved Terraform promotion, verify the model controller's actual
template/spec identity and current public V2V/transfer operations, artifacts,
download checksums and decode. Do not activate the LeRobot candidate solely on
this transport check. Rollback uses the previous generated model contract and
adapter/template under the same owner; leave model/snapshot PVCs intact.

The isolated preview can be removed by deleting its exact Deployment and
ConfigMap and stopping its localhost port-forward, once acceptance no longer
needs it. No node, quota, capacity or user workload changes are involved.

At 14:03 UTC the exact restored r7 preview passed V2V and edge-transfer MP4,
checksum and full reader-reopen checks. See `snapshot-media-compatibility.json`.
The original preemptible node became unreachable and was later cloud STOPPED;
the old task-owned preview resources were removed only after independently
confirming that fence. The subsequent restored preview was
`fs2-models/cosmos3-nano-media-snapshot-r20260917`, on one verified-free GPU of
existing regular reserved node `computeinstance-e00m0hsph76ajt9sdb`; its matching
ConfigMap was also task-owned. At root's request that exact Deployment/ConfigMap
pair and localhost port-forward were removed, pod absence was verified, and its
one GPU was freed. There are no remaining task-owned previews. The separate
`preview-cleanup.json` receipt preserves the historical runtime receipt unchanged;
all output media, datasets and failure receipts remain available privately.

The result supports the scoped media adapter/template cutover while preserving
r7. It does not establish robot-motion fidelity: V2V's partially conditioned
trajectory diverged in the final frames, and transfer preserved coarse layout
but stylized fine object details. Keep those limitations distinct from the
successful transport and dataset integrity checks.
