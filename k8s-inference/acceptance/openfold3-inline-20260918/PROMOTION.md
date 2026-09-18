# Prepared scientific OpenFold3 promotion (not applied)

Target only `openfold3-openbind`, the scientific-batch App. The separate legacy
HTTP `openfold3` runtime is untouched. Both CPU preparation and GPU inference use
the immutable candidate in `publication.json`; checkpoint, CCD, upstream revision,
workload resources, retry/cancellation policy, placement, and input/output contracts
are unchanged. The runtime recipe and execution identity are recomputed from the
current adapter/control-plane source, so a new control-plane image containing the
matching canonical profiles must accompany the execution-map change.

The new profile is **active, not qualified**. Its three-case isolated H100 receipt
is retained in `qualification.json`, and previous-image public/scheduler receipts
are cleared. Poor reference agreement is explicit in the profile and README.
Successful public replay will establish workflow behavior, not biological accuracy.

## Qualification reference rebase

The historical ten-model baseline includes the old OpenFold3 row and therefore
cannot describe the successor. `promotion.py` proves that all ten *other* current
scientific rows are byte-logically unchanged, constructs a digest of that exact
subset, and changes only those profiles' `qualification.execution_map_sha256`
references. Their source, images, execution identities, artifact bindings, states,
public/scheduler/H100 receipts and resource settings remain unchanged. Historical
activation fragments are retained; they describe the original baseline. The
original complete values/profiles and baseline mapping are retained in rollback
files and the rebase receipt, not falsely accepted by the new runtime loader.

## Exact prepared package

Read-only baseline capture: Helm166, confirmed `deployed`, 2026-09-18 22:41 UTC.
Private candidate:
`/home/tux/secure-handoff/openfold3-inline-candidate-20260918-BwsONH/promotion-candidate-r166-r2`.

- Scientific ConfigMap: `fs2-r927c465c6d-scientific-execution-b82de0f53dd0`.
- Exact rendered map SHA256: `b82de0f53dd0c273c3aa93321f8a903fecf9e06efa7dfd86be41cf703b94d3df`.
- Normal-load model-map SHA256: `9c7fef0e6cc9a50c0ff1cce66282aacc1811a2a0066d372b143b0b2ace328943`.
- Preserved sibling subset SHA256: `b69c2a4e830e69e7943b8c2cb479e8b0f3f88498b52009263d25ed5d76b4a001`.
- Validation receipt SHA256: `23ae2da9062127bbdaea17f1a8842aa3199f7adba1f888b50ef1c6cfa341fbc1`.

The actual `Registry.load`, serving bindings/promotions, admin bootstrap,
scientific profile loader, internal CPU canary, complete manifest renderer and
scheduler freeze pass for all23 serving registry entries and all11 scientific
Apps. Actual Helm rendering matches the reviewed map and retains all four live
snapshot records. Those historical snapshots do not qualify the new OpenFold3
image. These are offline startup checks, not current live health or new public
completion receipts.

## Owner-controlled cutover

1. Finish the serialized Cosmos/CXR changes. Recapture the then-current Helm
   values and serving ConfigMaps; do **not** apply the retained166 full values over
   a later release. Re-run this helper using the retained pre-change scientific
   profiles, fresh values/maps/deployment, unchanged scheduling bytes, and exact
   H100 receipt. Verify the raw scientific map remains identical if its inputs did
   not change, and re-run startup/render validation with the new serving maps.
2. Build the control-plane image from the committed successor profiles. Verify
   no active OpenFold3 operations before the coupled image/map cutover. Preserve
   other App settings and routes. No ModelDeployment image patch is applicable:
   this App executes through scientific Jobs.
3. Replace `scientificBatch.executionMap` as a **whole subtree** in the current
   complete release values. Use the root release helper's explicit scientific-map
   replacement or this helper's regenerated full `values.json`. Never combine
   old and new maps using two Helm `-f` files: recursive merge would retain the
   invalid historical baseline key. Apply only the reviewed complete values file.
4. Confirm actual startup, scientific discovery and unchanged sibling surfaces.
   Replay complete1ACB/1BRS/2PTC requests on the exclusively coordinated scientist09
   key, verify artifacts and scheduling receipts, and retain the original failed
   operation/cancellation. No new qualification is implied before those tests.
5. If rollback is needed, restore the matching prior CP image, scientific profile
   generation and execution map together, while preserving subsequent unrelated
   serving changes. The original full values are evidence, not permission to
   revert unrelated later releases.

`promotion.py` never writes to Kubernetes. It emits private complete values,
raw map, source profile/projection candidates, actual rendered ConfigMap,
validation and rollback records; it refuses an unexpected original image,
invalid historical baseline, or unbound old qualification reference.
