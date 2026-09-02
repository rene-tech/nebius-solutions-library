# Model onboarding compiler

Adding a model currently requires coordinated edits across the runtime catalog,
profile catalog, Kubernetes manifests, live-service inventory, semantic
qualification, cold-start evidence, and several derived contract indexes. This
prototype removes the first layer of repetition without pretending that a model
is qualified before it has evidence.

The input is one review-owned `ModelDefinition` JSON document with
`execution_mode` set to `http`, `scientific-batch`, or `hybrid`. The compiler
validates it and produces deterministic files in a separate staging directory;
it never edits the solution tree, Terraform inputs or state, and never talks to
a cluster or cloud API.

## Try the example

The checked-in example deliberately uses `.invalid` model/image identities. It
tests the workflow and output shapes; it is not a deployable model.

```bash
cd k8s-inference

python3 model-onboarding/compile_model.py validate \
  model-onboarding/examples/vllm-huggingface.json

python3 model-onboarding/compile_model.py dry-run \
  model-onboarding/examples/vllm-huggingface.json

python3 model-onboarding/compile_model.py generate \
  model-onboarding/examples/vllm-huggingface.json \
  --output-dir /tmp/example-7b-onboarding

python3 model-onboarding/compile_model.py check \
  model-onboarding/examples/vllm-huggingface.json \
  --output-dir /tmp/example-7b-onboarding
```

The two additional contract examples are deliberately non-deployable:

```bash
python3 model-onboarding/compile_model.py validate \
  model-onboarding/examples/scientific-batch-git.json

python3 model-onboarding/compile_model.py validate \
  model-onboarding/examples/hybrid-huggingface.json
```

`dry-run` prints paths, targets, sizes, and SHA-256 digests and performs no
writes. `check` returns exit status `1` for a missing, unexpected, or changed
file and performs no writes. `generate` refuses to overwrite an output tree
that contains files outside its exact bundle, so an operator note cannot be
silently deleted. Compilation also rejects an existing model ID, workload
service/placement, MCP tool, or manifest path; this first slice is strictly for
new-model onboarding, not in-place model upgrades.

The compiler requires `jsonschema`; the catalog test environment already
provides it.

## Outputs

An HTTP declaration produces:

| Staged file | Intended integration target |
| --- | --- |
| `catalog/runtime/models/<id>.json` | Canonical model record |
| `models/generated/<id>.yaml` | Scale-to-zero Deployment, PVC, Service, and NetworkPolicy |
| `projections/model-profile.json` | `catalog/profiles/model-profiles.json` merge input |
| `projections/live-service-route.json` | `all-models-live-services.json` merge input |
| `projections/catalog-index.json` | `catalog/runtime/catalog.json` sorted-set input |
| `onboarding-bundle.json` | Content-digest index and remaining promotion gates |

A `scientific-batch` declaration produces only
`projections/scientific-workload-profile.json` plus the bundle index. This is
intentional: a declaration must not synthesize a Kueue Job, serving route, or
credential before a separately reviewed BatchRun controller exists. A `hybrid`
declaration produces the HTTP files and this separate candidate batch profile.
The existing serving-binding consumer still accepts only HTTP backends, so this
change does not extend its schema with a batch shape that no runtime can honor.
Scientific candidate declarations may use a provider-neutral
`runtime.image.logical_repository` while the image is still `build-required`.
The deployment owner resolves that logical suffix against its configured
registry only after an immutable image digest has passed promotion; catalog
profiles never publish a regional registry hostname or accept one from a run
request. The cancer-immunotherapy declaration set is rendered with
`sync_scientific_profiles.py`; `--check` fails when either the canonical profile
set or source-qualified receipt set has drifted from those ModelDefinitions.

The model record is deliberately `unqualified`, non-invocable, conventional
startup only, with an unresolved weight artifact and blocked semantic
validator. The route projection does not modify any qualification list. A
declaration is desired configuration, never evidence.

For the vLLM adapter, validation joins the exact source and review revision,
requires the canonical
`https://huggingface.co/<namespace>/<repository>/tree/<revision>` review URL,
and joins the declared Service and `--port` and GPU count and
`--tensor-parallel-size`. It also requires one canonical `{MODEL_PATH}` token,
which becomes the catalog's immutable content-path token and the workload's
PVC-backed path. These joins prevent a valid-looking projection whose runtime
would listen elsewhere, use the wrong GPU arity, or silently redownload a
different model.

CPU, memory, and ephemeral-storage requests are the single resource source of
truth. The compiler derives catalog millicores/bytes and profile GiB from the
bounded Kubernetes quantities and rejects any limit below its request. Runtime
images, placement labels, and Hugging Face repository IDs also use closed
grammars before any artifact is rendered.

After reviewed fragments are integrated, use the existing
`catalog/runtime/scripts/refresh_scale_contracts.py`,
`catalog/runtime/scripts/refresh_golden_identities.py`, and
`components/control-plane/scripts/render_all_models_live.py` flows. This
compiler does not duplicate their contract hashing or release rendering.

## Target architecture

Keep one provider-neutral `ModelDefinition` as the canonical authoring
interface:

```text
ModelDefinition
    -> identity/catalog projection
    -> profile, route, and index projections
    -> runtime adapter -> Kubernetes workload
    -> qualification pipeline -> evidence-owned promotion
```

Runtime systems should be adapters, not competing canonical schemas. The
HTTP adapter is a bounded Hugging Face Deployment. The scientific adapter
captures an operator-owned stage DAG, retry/cancellation behavior, access
profile, request/result schema IDs, service classes, and MCP metadata while
remaining explicitly `candidate-unqualified`. Follow-up runtime adapters
can render KServe `LLMInferenceService`, llm-d, NVIDIA NIM Operator
`NIMService`, or NVIDIA Dynamo resources from the same identity, policy,
resource, protocol, and placement fields. Adapter-only options belong under a
typed runtime-adapter block; they must not leak into model identity or license
policy.

The control plane should ultimately consume a separately versioned catalog
artifact (for example, an immutable OCI artifact or content-addressed
ConfigMap) instead of requiring its application image to be rebuilt whenever a
model is added. That decouples frequent catalog changes from control-plane code
releases while preserving an exact catalog digest in Terraform and acceptance
evidence.

## Before this can replace the manual flow

This remains an onboarding compiler, not a promotion tool:

- It renders exact-revision Hugging Face HTTP models as ordinary Deployments
  with a provider-block PVC. Git sources and academic/gated access may be
  declared for scientific-batch candidates, but the compiler never embeds a
  credential or renders a Job. NIMService, KServe/llm-d, Dynamo, the BatchRun
  controller, bespoke media adapters, and local-NVMe localization remain
  separate implementation work.
- The generated init container expects `huggingface_hub` in the declared
  runtime image. A dedicated, promoted localization image should replace that
  assumption.
- It emits merge projections but does not modify central JSON. A follow-up
  transactional `promote --check` should merge sorted structures in memory,
  run the complete catalog/live-release suites, and write only if every gate
  passes.
- Artifact acquisition, provenance lock, runtime prerequisites, scale
  contracts, semantic two-request fixtures, compatibility bindings, model
  variants, golden identities, and cold-start/HTTP/MCP/elasticity evidence are
  still evidence-owned follow-up work. They are listed in every bundle index.
- The current canonical model schema retains a B300-specific historical
  compatibility field. The compiler sets it to `unverified`; actual workload
  placement remains accelerator-class/pool driven and can target H100 today.
  A future catalog schema revision should replace that legacy field with a
  general per-accelerator compatibility map before claiming a fully
  GPU-agnostic authoring model.

Run the prototype tests with:

```bash
python3 -m unittest discover -s model-onboarding/tests -p 'test_*.py' -v
python3 -m unittest tests.test_scientific_workload_contracts -v
```
