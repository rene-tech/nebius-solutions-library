# Native AlphaFold 3 adapter

`alphafold3` means the native Google DeepMind code and parameters under their
academic terms. It is never an alias for OpenFold3. The canonical profile runs
CPU data preprocessing and GPU inference as two separate stages.

## Command surface

The adapter targets the runtime image's own machine-readable command/IO
contract, `models/cancer-immunotherapy/images/alphafold3/contracts/af3-command-io-contract.json`
(schema `fs2-serve.nebius.ai/alphafold3-command-io/v1`). Both stages exec
`/alphafold3_venv/bin/python3 /opt/fs2/af3_runtime.py` directly - no shell, no
`args`, no wrapper:

| Stage | argv after the interpreter |
| --- | --- |
| `data-pipeline` (CPU) | `data --json-path <workspace input> --output-dir <workspace output> --reference-receipt <receipt> --threads 16 --cpu-request 16` |
| `inference` (GPU) | `inference --handoff-dir <materialized handoff> --output-dir <workspace output>` |

`tests/test_alphafold3_adapter.py` compares these templates to the contract
document, so a runtime successor that changes the surface fails the test rather
than the cluster. The retired `fs2-run-alphafold3` wrapper and its
`--input-json`, `--processed-json`, `--handoff-tar` and `/databases` surface are
gone; the runtime itself records that alias as unsupported. The adapter emits no
`--extra-arg`, so it cannot reach past the flags that keep the stages separated.

One argument is deliberately outside the contract:
`--runtime-localization-marker`. `StageInvocation` requires the controller's
localization marker path to appear in the argv of any stage that binds a runtime
artifact, and the runtime does not yet declare a flag for it. The test asserts
this is the *only* undeclared flag, so the obligation stays visible instead of
drifting. It is an open item for the runtime successor.

## Reference data: one root, no subPath

The publisher writes four things a preprocessing run has to reach, as siblings
under one root:

```
/reference-data/receipts/<bundle>/<revision>.json         the terminal receipt
/reference-data/<dataset_sub_path>/                       the expanded tree
/reference-data/<dataset_sub_path>/.fs2-manifest-sha256   the readiness marker
/reference-data/manifests/sha256/<manifest>.json          the manifest
```

So the CPU stage mounts the whole plane read-only at `/reference-data` with **no
Kubernetes `subPath`**. A subPath mount of the dataset alone would hide the
receipt and the manifest, and the tree could then not be verified against the
document that describes it. The execution-map schema pins `sub_path` to `null`
for this mount and the renderer refuses a narrowed root.

`dataset_sub_path` is `datasets/<bundle>/<revision>/sha256/<tree_sha256>`, and
the adapter derives it from the promoted digests rather than copying a string.
The test compares the result to `reference_data.derive_database_root` on a
receipt built by the producer's own `build_terminal_receipt`, so the adapter
cannot drift from the document the publisher actually writes.

The two reference identities stay independent: the content-tree digest names the
expanded tree, the manifest digest names the manifest. Neither is derived from
or defaulted to the other, and an equal pair is refused.

## Storage planes

AlphaFold 3 is a mixed-plane consumer, and the licence decides which plane each
artifact rides. The public v3.0 reference bundle comes from the shared read-only
reference plane as an operator-owned host root with no tenant claim. The licensed
parameters come from the tenant-private academic claim
`academic-assets-runtime-rwx` in `fs2-academic-poc`. There is deliberately no
single shared PVC holding both: putting the licensed bytes on the plane that
serves public reference data would place them outside the academic terms. The
adapter's stage closure keeps one artifact per stage, so no stage can hold both
planes at once.

## Stage separation and placement

The CPU stage binds reference data and no parameters. The GPU stage binds the
licensed parameters and no reference data - not the whole root and not a single
dataset. The runtime refuses a stage holding both bindings; the adapter never
composes one.

Both stages run in `fs2-academic-poc` as `fs2-academic-runner`, so the licensed
claim and the durable controller state stay in one namespace. Only the queue and
the pool differ:

| Stage | LocalQueue | ClusterQueue | Envelope | Accelerator |
| --- | --- | --- | --- | --- |
| `data-pipeline` | `academic-scientific-cpu` | `reference-data-cpu` | 16 CPU / 64Gi / 32Gi | none |
| `inference` | `academic-scientific` | `inference-accelerators` | 8 CPU / 64Gi / 64Gi | 1 × H100 |

The CPU pod carries no accelerator selector, so an MSA never occupies an idle
H100. 16 CPU / 64Gi / 32Gi is the accepted reference-data model requirement for
raw-input preprocessing: `jackhmmer` and `nhmmer` over MGnify, UniRef90,
UniProt, BFD and the RNA databases are the CPU-bound part of the model. The
smaller 6 CPU / 24Gi envelope belongs to the database *stager*, not to
preprocessing. Both MSA thread flags are driven by the stage's own CPU request,
because AlphaFold 3 otherwise derives its thread default from the node rather
than the pod and oversubscribes the cgroup.

The `reference-data-cpu` pool is currently smaller than this envelope; raising
it is owned by the general-CPU/reference-pool Terraform successor, and until it
is applied the CPU stage cannot be admitted live.

## Handoff

The data stage writes `<output_dir>/fs2-af3-handoff`, holding one data JSON per
fold job plus an `index.json` that records each entry by a path **relative to
that directory**, a byte count and a SHA-256. The collector verifies the schema,
the entry count, containment of every relative path, and each recorded size and
digest, then packages the directory into one tar written to a temporary name and
moved into place, so a partially written package is never collectable. The GPU
stage extracts it into its own workspace and is addressed with `--handoff-dir`.
No absolute path from the CPU pod is recorded or reused.

## Fold jobs

The runtime processes one fold job per GPU run: `--fold-job` is required once a
handoff holds more than one job, and an ambiguous handoff is rejected. Selecting
per run means one GPU work unit per fold job, and an execution plan admits
exactly one terminal invocation, so a request carrying several fold jobs is
refused at compile time instead of being compiled into a stage the runtime would
reject. Several fold jobs therefore need one run each until the terminal stage
can fan out. `fold_job_count` is bounded 1..64 in the parameter schema and the
adapter accepts 1.

`input_mode: enriched` is refused for the same structural reason: it would drop
`data-pipeline`, and the renderer requires a plan's stages to equal the
profile's stages in order, so such a plan could never be rendered. Enriched
input needs its own single-stage profile.

## Admission

Academic authorization is **deployment-bound**. The platform owner granted it
once for this deployment and it is recorded in the published profile's `access`
block (`profile: academic`, `operational_activation:
user-authorized-academic-poc`, `materialization:
restricted-quarantine-poc-authorized`, `license_gate_scope:
production-promotion-only`, `receipt_digest: null`). Formal institutional
acceptance is still required before production promotion, which is what
`production-promotion-only` records.

An ordinary caller therefore submits an ordinary request with **no per-request
and no per-input licence receipt**, and a public access context with a null
receipt digest is accepted. The adapter fails closed with
`AcademicDeploymentAuthorizationMissing` when any of those deployment fields is
absent or weakened; the test covers each field individually and the refusal at
the public submit boundary.

## Readiness

The authoritative runtime image digest is
`sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e`
(tag `3.0.4-85c4d205-r6`), published in `eu-north1` and pinned here. It carries a
passing H100 semantic acceptance on context `k8s-inference-h100`, project
`project-e00rene`, namespace `fs2-academic-poc`, pool `h100-reserved-8x`:
parameter load 405 arrays / 368,384,602 elements, a direct inference run in
58.44s producing six CIF files, and a passing handoff portability check.

The runtime clean successor carrying that digest is commit `2892f703`, built from
source `12d40c8f` with a matching SLSA VCS revision. **Its review is still
pending**, so nothing here calls the runtime final; this adapter consumes the
candidate's command/IO contract and its digest, and the reviewer's ACCEPT is what
would settle it. The predecessor `sha256:d8cf266e...0440` (`r5`) was rejected
because its SLSA VCS revision pointed at a dirty base, and the historical
`sha256:eaea560c...1887` is a stock upstream image retained only as past semantic
evidence; neither may be treated as final.

The adapter itself is **not deployed and not qualified**. The profile stays
`candidate-unqualified` with `route_exposed: false`, because this slice adds no
route, Job, Kueue workload or Kubernetes object and the remaining gates are owned
elsewhere:

- an `academic-scientific-cpu` LocalQueue in `fs2-academic-poc` routing to the
  `reference-data-cpu` ClusterQueue. No such LocalQueue exists yet; it is owned
  by the academic-assets module and the Kueue scheduling contract, and controller
  startup validates that cross-contract binding, so AF3 stays non-deployable
  while it is absent.
- a `reference-data-cpu` pool large enough for 16 CPU / 64Gi. The reviewed pool
  is currently about 7 CPU / 28Gi of schedulable capacity, so the preprocessing
  stage cannot be admitted until the general-CPU/reference-pool Terraform
  successor is reviewed and applied.
- a published AlphaFold 3 v3.0 reference bundle. The profile's reference
  requirement is still `supply_state: unresolved` with no tree or manifest
  digest, so the CPU stage has nothing to bind.
- one real end-to-end two-stage acceptance run through the controller, which is
  what would justify promoting the profile.
