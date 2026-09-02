# ESMFold2 adapter

`esmfold2` is the full, MSA-capable ESMFold2 identity. `contract.json` is a
canonical `scientific-workload-profile/v1`; `adapter.py` validates the
model-specific `parameters` inside `scientific-run-request/v1` and asks the
control plane's `catalog_adapter` to produce the internal `ScientificStagePlan`.
The request points to a canonical input manifest and names an optional A3M by
logical artifact name. Storage locations and runtime commands are not public
request fields.

The profile orders CPU input preparation before one-GPU folding. Image
packaging and real H100 qualification belong to runtime onboarding; the
recipe-derived candidate digest is not a registry receipt and no route is
exposed.
