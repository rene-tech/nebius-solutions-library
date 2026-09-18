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
* Ligand `41_7BKC_LIGAND` generation and filtering succeeded. Evaluation failed
  inside RF3's cuEquivariance import: Torch2.7 has `is_fx_tracing`, not the newer
  `is_fx_symbolic_tracing` names. RF3 swallowed that failure into an empty result;
  subsequently reading a `None` structure produced the misleading Biotite
  text-mode error. Failed single-file RF3 predictions now propagate the original
  exception rather than producing synthetic zero-confidence records.

The thin candidate keeps predecessor `f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca`,
upstream source54058860, Torch2.7.0+cu126, Triton3.3.0, RF3 version/checkpoint,
precision, seeds, scientific parameters and resource envelope. It pins the four
cuEquivariance packages consistently to0.9.0, within installed rc-foundry0.2.0's
declared `>=0.6.1` constraint. It does **not** disable accelerated kernels or
monkeypatch Torch. Exact wheels are hash-locked. The
[upstream0.10 release](https://github.com/NVIDIA/cuEquivariance/releases/tag/v0.10.0)
records the tracing-API rename; actual0.9 kernels must still pass the H100 gate.

`patch_variant_runtime.py` verifies both original source hashes before editing;
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

Before promotion, replay the four exact original ligand/AME payloads and a prior
successful protein-target payload on a task-owned H100 candidate. Inspect full
structural/metric outputs, real kernel execution and timing; preserve failures.
Then use parent-owned scientific profile/execution-map promotion, followed by
public API replay. No binding affinity, catalytic activity, or biological quality
claim follows from successful execution.
