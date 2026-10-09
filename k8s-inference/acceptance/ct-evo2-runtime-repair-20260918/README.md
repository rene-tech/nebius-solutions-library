# CT/Evo2 exact-image promotion preparation

These scripts **never apply** a ConfigMap, Helm release or owner spec. The parent
release owner controls live promotion and customer-path acceptance.

`capture_baseline.py` captures the deployed release, both controllers' matching
mounted contracts, four immutable maps and every ModelDeployment. It reads no
Secrets and fails if Helm changes during capture. The release-159 capture is
retained privately alongside the Evo2 qualification receipts.

`prepare.py` requires both complete isolated acceptance gates:

- CT: 54 exact-image, byte-verified semantic results, nine scans × three prompt
  modes × two unchanged cohorts. Points were derived from expert reference masks;
  this is research segmentation evidence, not independent clinical validation.
- Evo2: the complete 96-request replay, full-state and full-model numerical gate,
  responsive three-second production-timing probes and two additional serialized
  GPU calls. An overlapping duplicate must not execute again. Exactly 98 model
  receipts must exist, with no overlapping generation intervals.

Both runtime sources and the preparation script are Git-pinned. The four-map
candidate preserves all 18 sibling App qualifications, five speech Apps,
historical renderer/snapshot bundles, owner placement, replicas and other
operator settings. It validates every current owner and both new renders using
the real controller and gateway bootstrap boundary. The current baseline's
NV-Reason-CXR/SDXL accelerator warnings are retained, not silently qualified.

Changed runtime images receive new image-keyed compiler-cache paths and immutable
template identities. Every runtime/init/relay image changes consistently; model
weights, HOME, ordinary caches, PVCs, probe thresholds and resource limits do not.
Old snapshot references are removed from proposed owners, snapshot preference is
`Never`, and fast-start level is `Off`. Old snapshot records remain history; they
cannot qualify or restore the new runtime. New public MCP, cold-start, elasticity
and GPU-snapshot qualification remain false until separately tested.

Example (run from `components/control-plane`; protected paths supplied by the
release owner):

```sh
uv run --frozen pytest -q ../../acceptance/ct-evo2-runtime-repair-20260918/test_prepare.py
uv run --frozen python ../../acceptance/ct-evo2-runtime-repair-20260918/prepare.py \
  --baseline /private/release159-baseline \
  --expected-release 159 \
  --ct-evidence /private/ct-point-candidate-v2 \
  --evo2-evidence /private/evo2-long-prefix-qualification \
  --source-commit COMMITTED_PREPARATION_REVISION \
  --output /private/new-candidate
```

The output includes candidate maps/Helm values, both proposed owner specs,
original rollback values/specs, evidence hashes, successor runtime entries and
validation results. Recapture/rebase instead of applying over a changed release
or owner. A successful preparation is not a deployment or customer-ready verdict.

## Desired-state persistence

The exact reviewed successor descriptors from `ct-evo2-candidate162` are retained
in `catalog/runtime/deployment-runtimes/{evo2-40b,nv-segment-ct}-scientific-h100-20260918.json`.
The historical descriptors and canonical catalog records remain unchanged.
Terraform selects a descriptor by an operator's immutable image override
(`deployment.models.image_overrides` at the root, `model_image_overrides` in the
workloads stage); merely adding a candidate file changes no default or live App.
The deployed CT/Evo image digests must therefore also be retained in the release
owner's intended tfvars/desired-state handoff. No live Terraform values were
changed by this persistence step.

Dynamic ModelDeployment owners retain customer scaling, placement and other
runtime settings. A promotion creates proposals based on freshly captured owner
specs, never replaces them with catalog defaults. Archive the accepted proposal
and API update receipt; do not replay stale owner specs over subsequent admin
changes. GPU snapshots for either changed image remain independently unqualified.
