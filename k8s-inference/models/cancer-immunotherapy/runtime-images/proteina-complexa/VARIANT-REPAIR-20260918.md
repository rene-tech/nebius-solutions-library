# Ligand / AME runtime repair candidate

Status: candidate only; no production qualification is inherited from the
predecessor image. Public scientist04 failures are retained under the protected
campaign `cohorts/proteina-variants-r1/scientist-04` and exact stage logs under
`proteina-variant-diagnosis/failed-stage-loki.json`.

Two independent defects were observed:

* AME `M0584_1ldm` seeds7/42 failed before sampling. Its upstream ligand list is
  `[NAD, OXM]`, but Hydra supplies `OmegaConf.ListConfig`, which the pinned
  `LigandFeatures` list/tuple check treats as empty. Accepting `Sequence`
  preserves both names, their order, and existing multi-ligand bond validation.
  The v2 architecture was already active; no environment workaround is needed.
* Ligand `41_7BKC_LIGAND` generation and filtering exited zero. Evaluation failed
  inside RF3's cuEquivariance import: Torch2.7 has `is_fx_tracing`, not the newer
  `is_fx_symbolic_tracing` names. RF3 swallowed that failure into an empty result;
  subsequently reading a `None` structure produced the misleading Biotite
  text-mode error. Failed single-file RF3 predictions now propagate the original
  exception rather than producing synthetic zero-confidence records.
  Retained generation logs subsequently confirmed RF3 failures there too: the
  zero exit was **not** proof of successful folding or valid reward scoring.

The thin candidate keeps predecessor `f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca`,
upstream source54058860, Torch2.7.0+cu126, Triton3.3.0, RF3 version/checkpoint,
precision, seeds, scientific parameters and resource envelope. It pins the four
cuEquivariance packages consistently to0.9.0, within installed rc-foundry0.2.0's
declared `>=0.6.1` constraint. It does **not** disable accelerated kernels or
monkeypatch Torch. Exact wheels are hash-locked. The
[upstream0.10 release](https://github.com/NVIDIA/cuEquivariance/releases/tag/v0.10.0)
records the tracing-API rename; actual0.9 kernels must still pass the H100 gate.

`patch_variant_runtime.py` verifies all three original source hashes before editing;
the original lock and published image remain historical evidence. Tests execute
the pinned constructor and RF3 method, including actual OmegaConf inputs, before
and after transformation, without consuming GPU work. Existing runtime tests
remain required. The image build checks package versions and the required Torch
tracing symbol. Actual CuEq extension import needs `libcuda.so.1`; the CPU
BuildKit host cannot perform it (the unchanged predecessor fails identically).
The first unpublished build failure is retained. Extension import and real
kernel execution are mandatory on the H100 candidate, not inferred from build.

The first H100 candidate stopped before model work: the historical base's
`XDG_CACHE_HOME=/opt/fs2/artifacts/xdg` was not writable by UID10001. With a
task-local writable XDG cache, both CuEq0.9 accelerated extension imports passed
on H100 with Torch2.7 unchanged. The successor bakes the already documented full
Dockerfile `/tmp/fs2-home` cache policy and checks directory creation as UID10001.
This changes cache placement only, not the checkpoint mounts or model settings.

The second isolated candidate (`5e9cef0e…`) passed those import/cache checks but
exposed missing `Python.h` during actual Triton CUDA-driver JIT compilation.
Its generation exited zero because the upstream composite reward layer also
caught the RF3 failure and returned a zero total reward. This candidate is not
qualified; its logs and outputs are retained, and its task Pod/ConfigMap removed.
The successor installs Ubuntu's pinned `libpython3.12-dev=3.12.3-1ubuntu0.17`
(including matching distro patch dependencies; Python remains3.12.3), checks
real C-header compilation at build, and requires a tiny real H100 Triton kernel
before sampling. Configured folding-model errors now propagate through the
composite layer with their original cause rather than producing a synthetic
score. Successful score aggregation and weights remain unchanged. The isolated
matrix stops at its first failed stage instead of spending more model work on
an already demonstrated runtime defect.

Before promotion, replay the four exact original ligand/AME payloads and a prior
successful protein-target payload on a task-owned H100 candidate. Inspect full
structural/metric outputs, real kernel execution and timing; preserve failures.
Then use parent-owned scientific profile/execution-map promotion, followed by
public API replay. No binding affinity, catalytic activity, or biological quality
claim follows from successful execution.

## Completed isolated qualification (2026-09-19)

Exact candidate `e5e075237a680dc01ace45b97ecfcfb9f95f5897aa51f36164591a74778a9cd1`
completed all four stages for the four original ligand/AME requests and the
prior PD-L1 protein regression. Real H100 CuEq imports and a numerical Triton
kernel passed. Eleven exact-upstream/JIT regressions, 79 existing runtime
tests, six sampling-contract tests and six promotion tests pass.

The original explicit **100-step protocol is not a quality pass**: all eight raw
designs fail the independent basic C-alpha geometry check. All eight RF3/AF2
refolds pass that limited geometry check, but they must not be substituted for
the raw designs. Independent raw-to-refold RMSDs reproduce the emitted CSV:
ligand55.67–61.12Å, AME32.83/44.86Å, PD-L1 1.87/2.04Å. Finite coordinates and
completed stages alone do not establish usable designs.

Pinned upstream `configs/pipeline/model_sampling.yaml` uses400steps; its
100-step protein example is explicitly a Quick Local Test. Three **separate,
explicitly revised** requests changed only steps100→400 (plus distinct output
names), retaining target/seed/sample count/image/weights/resources:

| Case | Raw-to-refold CA RMSD | Basic raw/refold geometry | Cofactors retained |
| --- | --- | --- | --- |
| Ligand seed7 | 1.6663 / 0.8110Å | both designs pass | FAD |
| Ligand seed42 | 2.6197 / 1.4535Å | both designs pass | FAD |
| AME seed7 | 0.7363Å | pass | NAD and OXM |

This is a sparse computational comparison, not affinity, catalytic, clinical,
all-atom validity or general model-accuracy qualification. Shorter sampling is
supported as an explicit quality/runtime tradeoff, not declared invalid. The
hosted API already **requires** `diffusion_steps`; it never defaulted to100.
The standalone runtime's omitted `nsteps` remains400. The parameter description
now explains the upstream baseline; no default, limits or explicit requests
were changed. Public follow-up must explicitly request400 and retain the
original100-step outcomes.

The original five-case workspace was copied before its preemptible node became
unreachable. The first400-step comparison was interrupted after generation
started and has no claimed result; that one comparison was rerun on a new
task-owned H100. Subsequent comparisons used another observed free H100 slot.
All owned candidate Pods and ConfigMaps were removed; exact resource, GPU,
artifact and cleanup identities are in `qualification/variant-h100-20260919.json`.

`qualification/prepare_variant_promotion.py` consumes the exact live baseline,
reuses the existing scientific-successor and real Registry/bootstrap/render
validators, and changes only Proteina's four runtime images/identity plus
provably unchanged sibling qualification references. It preserves snapshots,
resources, artifacts and settings. The new image remains active/unqualified
pending ordinary public completion and scheduler receipts; the parent owns
promotion. No shared catalog map is edited by this helper.
