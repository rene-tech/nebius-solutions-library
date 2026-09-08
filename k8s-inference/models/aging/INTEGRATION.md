# Shared integration boundary

Model IDs: `altumage`, `phenoage`. Each needs an independent App record/public
route, preserving the platform's existing same-model duplication semantics.
Native endpoint: `/v1/predict`; readiness: `/v1/health/ready`; no separate auth,
MCP server, request database or queue implementation is needed.

Root coordinates shared edits and the release. The original model-local package
did not modify the shared platform. The subsequent extension implements the
CPU/catalog/controller interfaces described below; implementation and live
acceptance are recorded separately in
[CPU-MANAGED-APPS.md](../../acceptance/aging-20260908/CPU-MANAGED-APPS.md).
The implemented extension passed its public CPU/H100 HTTP/MCP and natural
zero-to-one-to-zero checks in
[PUBLIC-R05.md](../../acceptance/aging-20260908/PUBLIC-R05.md).
See [the release report](../../acceptance/aging-20260908/RELEASE.md) for exact
images, measured startup boundaries and retained earlier failures.

## Original CPU limitation (before this extension)

The MSA PDB70 portable CPU runtime proves native zero-GPU serving can be bound,
but it did not provide a dynamically managed ModelDeployment. The original source
assumed GPU capacity throughout the App settings/controller path:

- `deployment_runtimes.py::_record` accepts CPU only with a `reference-database`
  artifact baked into the image. CPU weights and a formula need truthful artifact
  kinds; they must not be disguised as a reference database.
- `model_deployment.py`: PlacementSpec requires accelerators per replica ≥1;
  PoolEnvelope requires accelerators per node ≥1; qualification likewise requires
  a positive accelerator count. Replica capacity divides by that count, and the
  renderer unconditionally writes the pool's GPU extended resource.
- `model_deployment_mutation.py` builds model choices and max-replica settings by
  dividing GPU budgets/counts. `model_deployment_publication.py` also requires
  a positive accelerator count. The CRD mirrors these contracts.
- `stages/workloads/model_controller.tf` identifies the runtime container through
  its GPU request and builds controller pools only from selected accelerator
  pools. Existing general CPU pools and their CPU/RAM admission envelope are
  available elsewhere, but are not wired into these dynamic serving settings.

## Minimum coherent extension, not a second controller

1. Add explicit CPU placement/resource capacity to the existing envelope/spec,
   preserving omitted/default fields and all current GPU identities. CPU replica
   headroom must use the actual per-Pod CPU/RAM request and configured schedulable
   pool capacity, not a fake GPU token or division by zero.
2. Reuse the existing renderer, hot/burst split, KEDA operation demand, startup
   retention, route publication and revision fence. CPU Pods omit GPU resources
   and use the declared CPU pool's labels/tolerations; GPU snapshot levels are
   not selectable for a CPU formula.
3. Wire the existing general CPU Terraform pool/queue contract into controller
   qualification, runtime-container selection and initial model configuration.
   Include the CRD, App mutation choices and publication DTO in the same change.
4. Add actual catalog source/variant/semantic records and deployment-runtime
   entries after root builds immutable images. Register native schemas from
   `contracts.py` and tests/fixtures without marking unmeasured hardware qualified.
   Add Terraform selection for both models; AltumAge's CUDA variant is separately
   qualified on a real assigned GPU before being advertised as supported.
5. Verify independently configured CPU Apps can start from zero, return two
   distinct public predictions, obey edited min/max replicas, retain operation
   and owner history, and return to policy-controlled idle. Measure external
   acceptance-to-result and Pod image/initialization separately. Compare AltumAge
   CPU and CUDA with identical synthetic samples/batch sizes and preserved failed
   runs; compare the two different clocks only as a compute/latency demonstration.

A temporary static CPU deployment is a valid runtime benchmark, but is **not**
acceptance evidence for dynamic App settings or scale-to-zero. No GPU should be
reserved for clinical PhenoAge to evade this missing CPU-control integration.
