# OpenFold3 inline-loader candidate qualification

This is isolated model qualification, not a production profile switch or public
API acceptance. The candidate replaces only `run_openfold3.py` from source
`84a466792feb69ac852ffdb6124c47706751646c`; immutable build identities are in
[`publication.json`](publication.json).

The original public 1ACB request stalled in the upstream multiprocessing tensor
queue feeder with the original 64 MiB `/dev/shm`. The single-query successor uses
`num_workers=0`, `prefetch_factor=null`, and `persistent_workers=false`, preserving
the upstream checkpoint, model settings, seeds, CPU/memory/GPU limits, and the
default shared-memory size. No larger shared-memory volume masks the failure.

## Reproduction

`prepare.py` consumes the retained original Pod and complete pinned complex
fixtures. It reuses the existing `render_semantic_job.py`, hash-checks the actual
checkpoint and CCD files, and parses the generated configuration through the
installed upstream `InferenceExperimentConfig` before model execution. The
preflight passes the same explicit checkpoint argument as the real CLI.

```sh
python prepare.py \
  --original-pod /private/pod-before-cancel.json \
  --fixtures /private/qualification-complexes-v1 \
  --image 'registry/image@sha256:IMMUTABLE_DIGEST' \
  --output /private/qualification \
  --name TASK_OWNED_POD \
  --pool h100-reserved-8x
```

The resulting ConfigMap and route-free Pod run 1ACB seed 1, 1BRS seed 7, and 2PTC
seed 42 sequentially on one H100. Use only an available slot in an existing pool;
the pool override changes placement, not resource requests or limits. The Pod
uses the original read-only hostPath artifact bindings and task-owned scratch
volumes. `fsGroup=10001` grants access only to those scratch volumes. It does not
modify shared cache or model files.

After `ALL_CASES_COMPLETE` (or `MATRIX_FAILED`), copy `/outputs` and `/work`, retain
the Pod/events/logs, then delete only the task-owned Pod and input ConfigMaps.
The final sleep is solely an artifact-collection window, not extra inference.

```sh
python verify.py \
  --directory /private/qualification \
  --fixtures /private/qualification-complexes-v1 \
  --evaluators /path/to/workbench/scripts/qualification
```

The independent check reopens all returned structures, verifies the original
input digest, upstream revision, requested seed and sample count, then checks
full two-chain sequence integrity and measures agreement with the experimental
reference. Reference RMSD and contacts are observations, not an accuracy
guarantee or a clinical/biological qualification threshold.

## Retained harness issues

The first placement became unschedulable before execution; it was deleted and
the same resource envelope was moved to a free slot in the existing reserved
8-GPU H100 pool. The next preflight omitted the real CLI's explicit checkpoint
argument. Upstream consequently tried to create its default checkpoint directory
inside a read-only model mount. Hash and 64 MiB shared-memory checks had passed,
but no prediction occurred. That harness failure remains retained separately;
the preflight was corrected without changing or rebuilding the candidate image.

Build contexts must retain readable/traversable source modes (`a+rX`) when
generated under a private umask. The exact private build script, registry
readbacks, SBOM/provenance and source hashes are bound by the publication receipt.
Authentication used an isolated Docker configuration and explicit `sandbox2`;
the default CLI profile was not changed.

## H100 result (2026-09-18)

All three cases completed on the exact published candidate, NVIDIA H100 80 GB
(driver580.159.04, PyTorch2.10.0/CUDA12.9), with67,108,864bytes of shared memory.
The actual checkpoint/CCD hashes, installed configuration, requested seeds,
output hashes and complete two-chain sequences passed independent validation.
This supports the shared-memory hang repair, **not structural accuracy**.

| Complex / seed | Runner seconds | Complex Cα RMSD (Å) | Native Cα contact recall |
| --- | ---: | ---: | ---: |
| 1ACB / 1 | 44.997 | 18.717 | 0 |
| 1BRS / 7 | 38.835 | 14.437 | 0.0556 |
| 2PTC / 42 | 40.537 | 18.882 | 0 |

The unmodified MSA-free/template-free inference policy gives poor experimental
agreement on these cases. No threshold was relaxed, model budget increased, or
reference output substituted. Runner times exclude scheduling, image download
and input preparation; they are not isolated GPU kernel times.

Protected evidence is under
`/home/tux/secure-handoff/openfold3-inline-candidate-20260918-BwsONH/qualification-r3`.
`independent-validation.json` SHA256 is
`97ab7fcc6ddf1ee6a114d82c1b328f7c8c751d859ac1f2624ce257254e8e1708`.
The complete sanitized `final-receipt.json` SHA256 is
`99e1e2672cb010b285d6031e42dd1e7fa903c10e532b942dc9c1444b1ccc62e6`.
All three task Pods (including earlier attempts) and all three input ConfigMaps
were verified absent after retaining logs, Pod/events, prepared inputs and
outputs. Production scientific-profile promotion and public replay remain
separate parent-coordinated gates.
