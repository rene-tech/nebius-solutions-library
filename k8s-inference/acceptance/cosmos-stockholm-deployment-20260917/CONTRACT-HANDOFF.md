# Complete Cosmos contract candidate — 2026-09-17

Prepared locally; this receipt does **not** assert publication, rollout, GPU inference, or customer acceptance.

`prepare_contract.py` reuses the existing speech/voice onboarding artifact shape (`ConfigMap` list, Helm `modelController` values, template references), CP typed contract validation/rendering, and the tracked scientific-fleet helper's Go/Helm JSON encoding. It does not introduce another runtime contract or modify the existing onboarding helpers. The speech executable itself must not be used for Cosmos: it hardcodes speech identities and changes policy/minimum replicas.

## Frozen inputs and candidate

- Source: `d8f0f0f03e2ba0ae515e6975f7bf02dabec249ee`; committed `models/general-media/k8s/cosmos3-nano.yaml` SHA-256 `46a011069849b08212e753d6036c29e93f80a7032299aecd0517c5067dd5b447`. The helper verifies the current file equals that committed source. Terraform's existing image, placement, replica, shared-cache and supported-resource transformations remain authoritative; the raw YAML is not the renderer bundle.
- Protected Terraform plan SHA-256: `9753135f6ffb77fe93c243ef620dd8f0d432ed2f5e2b7d667f95bb85a233fd1f`. Only its updated Cosmos record is added to the complete live voice baseline.
- Old Cosmos reference retained: `cosmos3-nano.legacy-v1`, `sha256:b2ce3b2351242330d1bb17f701968cc044bc244ff760567faaadbe915bb55214`.
- New reference: `cosmos3-nano.stockholm-v2`, `sha256:a4c96a343622a0e809e61f132c71032dd6e845c9ea7d846563cdb0ce36cf0fab`.
- Candidate: **20 model identities, 23 bundle revisions**. All 22 existing bundle objects are retained exactly. All other 19 model qualifications, including all five voice models, are unchanged. No pool, quota, tenant, image override, scale or other customer setting is changed by the merge.
- All 21 captured current ModelDeployments remain `accepted`; both old and proposed Cosmos specs render six resources. This is local compatibility evidence, not a fresh live mutation preview or inference result.

Exact proposed Helm values:

```yaml
modelController:
  infrastructureEnvelopeConfigMapName: fs2-cosmos-envelope-608b68ad9c0f32b2
  rendererBundlesConfigMapName: fs2-cosmos-bundles-d556dcfd58d35b29
```

| Identity | SHA-256 |
| --- | --- |
| Envelope ConfigMap canonical data | `608b68ad9c0f32b29461395113772f0d1acd8d9ac68d91976915c850b2fce876` |
| Bundle ConfigMap canonical data | `d556dcfd58d35b29a00eb89bf0f3c3e1a7e013a6cf35aeaa2fe87d232790ca32` |
| Envelope revision | `88d6c4d3f723f31310589934b585f7205ab25b7ddf7f950ea1a28413c5e53a8c` |
| Rendered `configmaps.json` file | `ed86519029f3e8b00c6d68f9d89a1cbeda358ea77fda57c5c29ebafbc7fecc93` |
| `validation.json` file | `134d9742f5118c505da70a4e62c6180071f9b214147afa11ea458959f54204ee` |

Private outputs are under `/home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/cosmos-complete-contract-candidate-final/`: `configmaps.json`, `values.json`, `template-refs.json`, complete original/proposed App specs, and a hashed validation receipt. They contain no credentials but retain operational configuration, so they are not repository fixtures.

## Reproduce without publishing

From `k8s-inference/components/control-plane`, with the existing project virtual environment:

```bash
PYTHONPATH=src .venv/bin/python ../../acceptance/cosmos-stockholm-deployment-20260917/prepare_contract.py \
  --plan /home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/cosmos-model-contract.plan.json \
  --live-configmaps /home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/live-voice-contracts-readonly.json \
  --modeldeployments /home/tux/secure-handoff/cosmos-stockholm-rollout-20260917/live-modeldeployments-readonly.json \
  --source-commit d8f0f0f03 \
  --output /an/operator-selected/new/private/directory
PYTHONPATH=src:tests .venv/bin/pytest -q ../../acceptance/cosmos-stockholm-deployment-20260917/test_prepare_contract.py
```

The output directory must not already exist. Preparation has no network or Kubernetes mutation path. Eight focused tests pass; Ruff passes. Tests cover preservation, old/new render compatibility, deterministic names, sibling drift, missing voice models, resource-digest tampering, non-template Cosmos changes and duplicate identities.

## Owner-controlled promotion and rollback

1. Re-read live envelope/bundle references and the Cosmos App before mutation; stop if they drifted from the captured baseline. Retain the current `fs2-voice-envelope-f0db5d7a98b7c484` and `fs2-voice-bundles-76b9ca3c4f5bea28` maps. Publish the two additive immutable maps, then have the release owner switch CP and model-controller references together using retained Helm values. Wait for both consumers to load the candidate. No CP image change is needed.
2. Use the existing owner API, not a raw CRD patch. Current captured Cosmos is generation 14, UID `6cdbf9c3-77b6-41f4-a931-30672611063c`, spec digest `sha256:f0157868064ea9f9014091b58b40a66bbd084c89fa27c96ab930e48d2e8923a3`.
3. `POST /admin/api/v1/model-deployments/cosmos3-nano:drain` with a fresh owner ETag and unique idempotency key. Wait for that revision's matching spec digest, `Cold` phase and desired/ready/available replicas all zero. **Enabled plus min=0/Cold alone is insufficient:** runtime material changes require a non-Enabled current lifecycle and observed cold-cutover proof. Let any active work drain normally.
4. Fetch the fresh drained ETag. Use `:plan-preview` and `:apply` with the original complete spec, changing only `runtime.templateRef` to the new name/digest. Restore the original lifecycle `Enabled` in this proposal. Keep min=0, max=4, idle=30s, cooldown=30s, polling=5s, targetQueueDepth=1, empty warm windows, and the original `Recreate` rollout policy. A stale captured proposal is not permission to overwrite later operator changes. The root-owned `promote_template.py` implements this bounded owner sequence.
5. Verify reconciliation and real public Cosmos inference separately before recording customer readiness. Typed acceptance and a successful Helm rollout do not establish media correctness.
6. For rollback, drain and owner-preview/apply the original template while the **additive candidate is still mounted**, restoring original lifecycle/settings. Only after the old Cosmos spec is observed may Helm point back to the previous voice maps. Those older maps do not accept the new digest. Keep both generations of maps through the rollback window; do not delete maps merely because Terraform says `create_before_destroy`.

## Canonical Terraform reconciliation boundary

The inspected Terraform before/after documents contain only 15 model identities. Directly switching to its generated maps would withdraw all five voice qualifications; this candidate deliberately avoids that loss. The existing Terraform plan replaces its old baseline maps, not the newer voice maps currently mounted by CP/controller. No claim is made that applying that plan alone manages this complete candidate or the Helm handoff atomically.

The candidate is an explicit retained-release overlay, not Terraform state convergence. Before a later canonical contract apply, reconcile all 20 identities and required retained revision/name mappings into the existing declarative model selection/contract generation, then re-plan and compare with the live complete contract. Do not use broad imports/state changes or remove retained revisions to make the plan appear clean. No reconciliation apply is performed by this helper.
