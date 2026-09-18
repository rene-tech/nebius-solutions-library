# Additive LeRobot activation

The canonical scientific profile, request schema and execution row are packaged
in the control-plane image. The CPU worker is separately pinned by its registry
manifest digest. `active` enables ordinary HTTP and typed MCP admission for keys
with both the LeRobot and Cosmos model grants; it does **not** mean qualified.
During onboarding the public-completion and scheduler-eligibility receipts stay
null. Admin discovery explicitly reports incomplete qualification.

The first release147 public MCP dataset attempt is retained as a
[failed integration receipt](debug-failure-r147.json), not qualification. Its
attempt-scoped upload succeeded and generation was admitted under the parent's
concurrency-one policy, but the worker misread the bare operation response and
failed before polling. Parent fencing cancelled the activating child; no GPU
generation or output dataset completed. Corrected worker publication, another
real public dataset result and pinned-reader validation are required before
promotion.

The CPU parent requests 2 CPUs, 16 GiB RAM and 32 GiB ephemeral storage, with
4 CPU/24 GiB RAM/32 GiB storage limits. Its ordinary scientific companion and
existing general-CPU queue remain unchanged. The worker rejects source bundles
over 5 GiB, expanded datasets over 8 GiB, and requests whose conservative
workspace estimate cannot fit 32 GiB before contacting Cosmos. Each output is
bounded to 5 GiB. See the [worker limits](../runtime/README.md).

Existing ten scientific profile receipts are retained unchanged. Their original
whole-map digest is represented by `qualification_baselines`: the ordered set of
old model IDs must reconstruct the **exact** historical map hash. Any changed or
removed image, stage, mount, resource, namespace, environment or identity fails
loading. A new model cannot borrow an older model's proof. Current run receipts
still bind the complete current execution-map recipe; full configuration
identity additionally includes the snapshot registry and baseline metadata.

The rollout owner captures current Helm values and the scheduler ConfigMap's
exact bytes, then renders an additive overlay:

```sh
python3 render_overlay.py \
  --baseline-values /private/baseline.values.yaml \
  --baseline-scheduling /private/baseline.scheduling.exact.json \
  --output-dir /private/lerobot-activation-candidate
```

The renderer refuses changed/deleted old model rows, preserves every live
snapshot bundle, checks the scheduler's captured byte hash, and adds only the
LeRobot eligibility row using the existing Cosmos eligible pools. It emits a
new content-addressed scheduler ConfigMap plus a `scientificBatch`-only Helm
overlay. Apply neither an old whole values file nor an older reduced model map:
the live speech, storage and serving catalog must remain intact. The owner
applies the new ConfigMap and rolls out the matching control-plane image.

The Terraform facade uses the same strict baseline reconstruction in
`scientific-execution.tf`; it keeps the raw Helm map hash separate from the
current recipe hash. A scientific-only App also needs the per-cluster
`deployment.scheduling.model_eligible_pool_ids` entry. For this H100 deployment,
`cosmos3-lerobot-augmentation = ["h100-reserved-8x"]` preserves the existing
Cosmos eligibility declaration. This selects no GPU for the CPU parent; each
delegated Cosmos child retains the native model's own serving placement.

No local test or historical preview is a completed public LeRobot workflow.
After deployment, retain actual ordinary-key upload/submit/child operations,
downloaded bundles, full pinned-reader reopen/decoded frames, action/timestamp
comparisons, idempotence and cancellation evidence. Promote only with those
actual receipts, then complete the unchanged customer cohorts. Numeric-record
preservation is not proof of visual/action fidelity or downstream training
quality; transfer and video-to-video retain their documented limitations.
