# Molecular runtime repair: prepared promotion

This directory prepares, but does not apply, the two repaired runtime contracts.
Do not infer public-route, cold-start, elasticity, snapshot, medicinal efficacy,
or experimental protein-function qualification from isolated runtime tests.
MolMIM is deliberately excluded: its retained feasibility failures still require
work before a production recommendation.

## Exact candidates and evidence

Both images use the existing registry prefix
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/`.

| Runtime | Candidate image SHA256 | Runtime source | Isolated H100 result |
| --- | --- | --- | --- |
| GenMol | `97c82cfacca3845ce3c9f50f430bb02b52747d97a76177294add531c42b26979` | `487ddedf4e5d589dd37fb830b64580756dc5c7fe` | 38 requests, exact 338 returned molecules; one actual refill; two explicit uniqueness probes |
| ProteinMPNN | `81acc477690e8c020e7499f474cd13c1cf4e21f1d210d10cda57f65d3793e227` | `673409230817fc4a1578fada4bc06fa7a3f1671d` | 36 requests, 144 sequences; real residue-numbering gaps and complete-backbone regressions |

The private qualification root is
`/home/tux/secure-handoff/h100-candidate-qualification-20260918-QYy5d8`.
Its `genmol-http/summary.json` SHA256 is
`a36b2e47af02f1417a7f37d0bda5808e6b4292186c7464210d1d6cc77dbd2cf1`;
`proteinmpnn-http/summary.json` is
`c06ce74b653b500f8edb61f3d026750f0619bcca59d4391ac177c2d78fe31460`.
The observed accelerator was H100 80GB, driver 580.159.04. Test pods were removed.
The new deployment-runtime files retain historical evidence with its original
source identity; only the appended exact-image receipt qualifies these repairs.
The schema's `live-qualified` evidence outcome refers to these isolated live GPU
tests, not a completed platform release. The explicit qualification flags leave
route/HTTP-MCP/cold-start/elasticity false and their receipts null.

## Read-only preparation

Retain read-only JSON captures of the currently mounted infrastructure-envelope
and renderer-bundle ConfigMaps as a Kubernetes List, the current routes ConfigMap,
the mounted admin-configuration baseline ConfigMap, and all current
ModelDeployments. These may contain deployment configuration and
belong in a private directory, not the repository. The initial reviewed captures
are under `/home/tux/secure-handoff/molecular-promotion-20260918-3OiTaJ`.

From `k8s-inference`, with the control-plane virtual environment:

```sh
components/control-plane/.venv/bin/python acceptance/scientific-runtime-repair-20260918/prepare_promotion.py \
  --source-commit EXACT_COMMITTED_CANDIDATE_REVISION \
  --live-configmaps PRIVATE/live-configmaps.json \
  --live-routes PRIVATE/live-routes.json \
  --live-admin-configuration PRIVATE/live-admin-configuration.json \
  --modeldeployments PRIVATE/modeldeployments.json \
  --output PRIVATE/new-candidate
```

The helper checks that both successor JSON files match the explicit commit,
reuses the existing voice/Cosmos resource-digest format, and produces private
immutable ConfigMaps, a four-reference Helm values delta, proposed App specs,
original rollback App specs, and a hash-bound validation receipt. It has no
Kubernetes, Helm, Terraform, or admin API mutation path.

The baseline changes only the two selected model artifact identities (image,
acquisition and provenance descriptors); operator settings and all sibling rows
are retained. Preparation invokes the gateway's actual bootstrap validation.
The first attempted rollout omitted this fourth dependency and its new gateway
failed startup with a canonical-catalog mismatch. Existing gateway replicas
continued serving while Helm rolled back. Preserve that failed release receipt;
renderer validation alone was not a sufficient deployment acceptance test.

The complete envelope retains all 20 model qualifications, including the five
speech models. Only GenMol and ProteinMPNN gain candidate image permission; the
other 18 qualifications and all historical bundles/snapshot records are kept.
The static lean-route bytes are unchanged. Only these two deployment-runtime
records and qualification-projection rows change; other rows and their historical
observation timestamp are not refreshed. All 21 captured ModelDeployment
validation dispositions must remain unchanged, and both proposed specs must
validate and render the exact new runtime images.

GenMol receives one additive `genmol.scientific-repair-20260918` template. It
changes the primary image and rekeys six image-derived compiler-cache paths;
the weights, PVC, resources, probes and all other settings remain unchanged.
ProteinMPNN needs an image-only change and retains its old template. The captured
GenMol cache tier is SharedFilesystem; ProteinMPNN is NodeLocal. Both retain
`snapshotPreference: Never` and FastStart `Off`. Old GenMol snapshot evidence is
still bound to its old image; it is not transferred to the new digest.

## Coordinated promotion and recovery

Only the release owner executes this sequence after review and after unrelated
customer workloads/qualification runs are ready for it:

1. Recheck current App specs/ETags and mounted contracts against the captured
   inputs. If they changed, regenerate and review; do not overwrite concurrent
   configuration. Create the four new immutable ConfigMaps additively, retaining
   all old maps. This alone does not change any route or running model.
2. Immediately before cutover, drain only these two Apps through the existing
   `/admin/api/v1/model-deployments/{name}:drain` API with current ETags. Wait for
   terminal in-flight operations and each matching observed revision/spec digest
   to be Cold with desired/ready/available replicas zero. No early drain while
   images, contracts or the release are still being prepared.
3. Upgrade the existing Helm release using all retained values and only the four
   generated ConfigMap references. Wait for the gateway and controller to load
   them. The canonical runtime identity check intentionally prevents an old
   runtime image from publishing against a new selected record; drained Apps can
   therefore be unavailable during this bounded coordinated cutover.
4. For each App use the existing `model-deployments:plan-preview` and `:apply`
   owner APIs with the current drained ETag. Apply the saved original spec with
   only the candidate image and, for GenMol, the new template reference. Restore
   its original lifecycle, min/max replicas, queue/cooldown/placement/settings.
   The captured original minimum is one; do not replace it with an arbitrary
   zero. Do not patch generated Deployments or bypass the owner API.
5. Verify controller observation, exact image digests and Ready endpoints, then
   perform separately authorized public HTTP/MCP and customer-shaped checks.
   Record their actual outcomes before changing any qualification claim.

If recovery is needed, coordinate restoring the old runtime selection ConfigMap
with the saved original App specs/images through the same owner APIs. The old
templates and image permissions remain available. Do not pair a new image with an
old snapshot or silently re-enable an unsupported startup mechanism. This helper
does not automatically retry or roll back an interrupted promotion.

The release owner must also retain the two image pins in the actual protected
H100 Terraform profile after locating it from release/state metadata. A future
canonical apply must reproduce the complete live voice/media contracts and the
new GenMol image-derived cache template. This staged additive Helm contract is
not a zero-drift Terraform claim; a broad apply of an older reduced model bundle
would lose live additions and is not part of this procedure.

## Local verification

From `components/control-plane`:

```sh
.venv/bin/pytest -q tests/test_molecular_runtime_promotion.py tests/test_deployment_runtimes.py
.venv/bin/ruff check tests/test_molecular_runtime_promotion.py ../../acceptance/scientific-runtime-repair-20260918/prepare_promotion.py
```

Result: 35 tests passed and Ruff passed. Existing pytest temporary-directory
cleanup warnings from another PostgreSQL test directory do not alter these test
results; no unrelated directory was removed.
