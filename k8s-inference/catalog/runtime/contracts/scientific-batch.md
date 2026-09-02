# Scientific batch execution contract

This directory defines the review boundary for asynchronous scientific model
workloads. It does not deploy a controller, submit Kubernetes resources, expose
a route, or qualify a runtime. Checked-in exact source revisions are
source-qualified, while every generated workload profile remains explicitly
`candidate-unqualified` and non-invocable.

## Runtime shape

| Model identity | Preferred interface | Scheduling unit | Initial semantic gate |
| --- | --- | --- | --- |
| `proteina-complexa` | scientific batch | Independent Kueue Jobs for generation, filtering, evaluation, and analysis | Expected stage manifests exist; structures parse; scores are finite; accepted outputs trace to requested candidates |
| `boltzgen` | scientific batch | Independent, shardable Kueue Jobs per documented pipeline stage | Output directory contract, parseable structures, finite confidence/design metrics, requested design count accounted for |
| `mosaic` | scientific batch | Workflow-expanded independent Jobs; JobSet only for an actually coupled distributed stage | Resolve the provisional `escalante-bio/mosaic` backend, validate typed composite configuration and declared output inventory |
| `bindcraft` | scientific batch | Independent trajectory/design Jobs followed by scoring/ranking Jobs | Parseable designs, requested target-chain identity, finite ranking metrics, complete trajectory accounting |
| `rfdiffusion` / `upstream-v1-1-0` | scientific batch campaign variant; the existing canonical `rfdiffusion` NIM stays intact | Independent design shards; JobSet only for an explicitly gang-coupled runtime | Contig/motif contract honored, parseable structures, requested design indices complete |
| `esmfold2` / `esmfold2-fast` | scientific batch candidate | Independent Jobs; Fast accepts only a protein single sequence | Input sequences map one-to-one to parseable structures with finite confidence values; Fast rejects MSA, ligand, nucleic-acid, modified-residue, and CCD semantics |
| `protenix-v2` | scientific batch | Independent prediction Jobs, optionally preceded by separately admitted data/search stages | Input entities map to predicted structures; confidence/result files parse; exact v2 backend and artifacts match execution identity |
| `alphafold3` | scientific batch | CPU/data-pipeline and GPU-inference Jobs as separate stages; JobSet only for a coupled implementation | Official input/output contracts parse; requested seeds/samples are accounted for; confidence/structure files are internally consistent |

Ordinary Kueue Jobs are the default because most designs, trajectories, seeds,
and input records can complete independently. JobSet is reserved for a stage
whose replicas must be admitted and run as one gang. A future BatchRun
reconciler should commit and validate one stage's output manifest before
creating downstream work, allowing the earlier stage to release quota.

## API and MCP mapping

The intended API is:

- `POST /v1/models/{model_id}/variants/{variant_id}:submit` with
  `fs2-serve.nebius.ai/scientific-run-request/v1` and an `Idempotency-Key`
  header;
- `POST /v1/models/{model_id}:submit` is only a convenience alias when the
  catalog declares exactly one default variant;
- `GET /v1/operations/{operation_id}` for durable state and the terminal
  `scientific-run-result/v1` document;
- `GET /v1/operations/{operation_id}/events` for ordered phase/attempt events;
- `DELETE /v1/operations/{operation_id}` for idempotent cancellation.

MCP should expose a thin typed submit tool and operation get/cancel tools over
the same API. A candidate profile is discoverable but has `invocable=false`.
The MCP server must not create Jobs itself or reinterpret free-form prompts as
runtime command lines.

An idempotency record binds tenant/principal, model ID, variant ID, operation, canonical
request digest, and `Idempotency-Key`. Repeating the tuple returns the original
`operation_id`; reusing the key with different content returns a conflict.
`model_id` and `variant_id` are retained in the operation, execution identity,
workload, event, and telemetry joins. Route/default/MCP identities must be
collision-checked before exposure. `operation_id`, `batch_id`, and `workload_id` remain stable across retry, while
each execution receives a new `attempt_id`. The result records Kueue Workload,
Kubernetes Job/Pod/Node, and GPU identities without placing long IDs in labels.

`service_class` is caller-selectable. Queue, priority, pool preference,
accelerator resource, runtime image, argv, environment, retry ceiling, and
checkpoint policy are operator-owned. Model-artifact identity and runtime-
recipe identity remain separate from mutable scheduling and access decisions.

The immutable scheduling snapshot retains logical `tenant_queue` and
`model_lane` plus one closed scheduling decision for every DAG stage. Each
stage decision records its resource class, exact resolved ClusterQueue,
LocalQueue, workload priority class/value, ordered provider-neutral pool
preferences, accelerator resource/count, queue/execution ceilings, and
checkpoint/preemption modes. The actual Kueue admission is recorded on each
attempt when admission occurred: resolved pool, admitted ResourceFlavor,
accelerator resource/count, and admission time. An explicit `null` records an
attempt that ended before admission; terminal success cannot use that state.
This stage/attempt split preserves one policy decision while allowing a retry
to be admitted to a different compatible pool or flavor without rewriting
history. `stage_id`, `shard_id`, and `attempt_number` provide the exact join.

A terminal result can contain up to 64 stages, 1,024 independently admitted
work units per stage, and 10 attempts per work unit. The closed result schema
therefore bounds `attempts` at 655,360 instead of ten; operational APIs should
paginate their attempt and event views while retaining the complete durable
terminal record. A future reconciler must propagate
`fs2.nebius.ai/{model-id,workload-id,attempt-id,tenant-id,service-class,local-queue}`
labels to every Job, JobSet, and Pod; long or unrestricted identities remain in
annotations and the operation store.

## Artifact and checkpoint rules

Inputs and outputs are immutable content-addressed manifests. A public
`ArtifactPointer`
contains only artifact ID, SHA-256, byte size, media type, and optional
compression. Local paths, bucket locations, credentials, and presigned URLs are
resolved by an authorized artifact service and are not durable API fields.
Terminal success requires a committed output manifest and a passed
model-specific semantic receipt.

The artifact service may retain a richer internal record containing tenant,
operation, attempt, storage-key, access, and creation metadata. That record is
not the `scientific-artifact-pointer/v1` API shape: it must be authorization-
checked and projected before inclusion in a request/result or MCP response.

Startup acceleration (image cache, model cache, or GPU-memory snapshot) is
separate from scientific progress checkpointing. `checkpoint_mode=restart`
means a preempted attempt restarts from committed stage inputs;
`checkpoint_mode=resume` is valid only after the exact backend proves and
qualifies a resumable checkpoint format. Cancellation stops new work, suspends
admitted Kueue work where supported, terminates active attempts using the
declared grace behavior, and preserves already committed immutable artifacts.

## Access-gated native backends

Native BindCraft with academic PyRosetta and native AlphaFold 3 remain in
scope, but their access facts are admission prerequisites rather than model
performance identity. A controller must fail closed before Job creation unless
the tenant has an exact access receipt; credentials never appear in a profile,
request, result, label, event, or log. Open binder and OpenFold-family
alternatives must use explicit variant IDs and backend identities—there is no
silent fallback or substitution.

## Candidate source evidence

`scientific-source-candidate-receipts.json` is generated from the same canonical
ModelDefinitions as the workload profiles and pins the source-qualified exact
revisions reviewed on 2026-09-02. Source qualification does not qualify a
runtime, artifact materialization, license acceptance, hardware tuple,
semantics, or production readiness.
Primary project documentation supporting the proposed workflow classification:

- [Proteina-Complexa](https://github.com/NVIDIA-BioNeMo/Proteina-Complexa)
  documents separate generation, filtering, evaluation, and analysis stages.
- [BoltzGen](https://github.com/HannesStark/boltzgen) documents staged,
  restartable design campaigns and their output directories.
- [mosaic](https://github.com/escalante-bio/mosaic) describes a composable JAX
  protein-design framework; the requested semantic identity is still
  provisional.
- [BindCraft](https://github.com/martinpacesa/BindCraft) documents trajectory
  generation and its PyRosetta installation/access dependency.
- [RFdiffusion](https://github.com/RosettaCommons/RFdiffusion) documents
  independent design generation and contig/motif inputs.
- [ESM](https://github.com/Biohub/esm) is the exact source family for the two
  requested ESMFold identities; their profiles pin the distinct Hugging Face
  repositories, revisions, and complete offline file sets.
- [Protenix](https://github.com/bytedance/Protenix) documents file-oriented
  structure-prediction inference.
- [AlphaFold 3 performance guidance](https://github.com/google-deepmind/alphafold3/blob/main/docs/performance.md),
  [output contract](https://github.com/google-deepmind/alphafold3/blob/main/docs/output.md),
  and [model-parameter terms](https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md)
  support separating data/inference stages, semantic output validation, and
  access admission.
- [Kueue overview](https://kueue.sigs.k8s.io/docs/overview/) and
  [JobSet integration](https://kueue.sigs.k8s.io/docs/tasks/run/jobsets/) define
  the queue/admission and gang-workload primitives used by this contract.

## Implementation boundary and next tests

This slice owns schemas, examples, candidate receipts, and deterministic
onboarding projections. A runtime implementation should be a separate change
with these exact acceptance points:

1. Add a versioned BatchRun API/controller and generated Job/JobSet templates;
   unit-test DAG progression, access rejection before Job creation, retry,
   preemption, cancellation, and output commit ordering.
2. Add control-plane submit/get/events/delete adapters and MCP tools; contract-
   test idempotent replay, key-content conflict, tenant isolation, schema
   validation, and absence of caller-controlled execution fields.
3. Add per-model parameter schemas and semantic validators; run two distinct
   retained requests for every exact source/artifact/image/GPU tuple.
4. Add artifact-service authorization and commit protocol tests, including
   digest mismatch, partial upload, expired location, and cross-tenant denial.
5. Add Kueue integration tests for independent Jobs, the few proven gang
   stages, service-class ordering, fair sharing, preemption, and return to zero.
6. Add end-to-end observability assertions keyed by `attempt_id` and Pod UID,
   including queue, image pull, artifact localization, weight load, active GPU,
   allocated-idle GPU, checkpoint, upload, and teardown phases.

Do not add a batch variant to `serving-bindings.schema.json` until the executable
consumer and controller can honor it; schema acceptance without a runtime
would turn an honest candidate into a misleading route claim.
