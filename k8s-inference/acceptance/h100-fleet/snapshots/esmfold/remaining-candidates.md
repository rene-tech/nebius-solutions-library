# Remaining primary scientific snapshot candidates

This is a bounded implementation assessment, not a new GPU qualification.
The three models already have valid native scientific workflows. Their
snapshot options must remain separate from normal serving until fresh-Pod,
changed-input proof succeeds.

| Model | Cached container → initialized, median (n=3) | Useful next implementation seam |
|---|---:|---|
| RFdiffusion | 34.87 s | Retain the immutable model/checkpoint in a request-ready worker, but rebuild each request's sampler and target/contig state. The existing `Making design` marker is after sampler construction and is already request-bound; capturing there is not a reusable model snapshot. |
| Proteina-Complexa | 30.26 s | Preload the exact model plus autoencoder and required LoRA state independently of Hydra's per-request generation config. Recreate target, seed, output root and dataloader for every call through the original wrapper. Keep each supported pipeline/variant explicit. |
| BoltzGen | 36.05 s | Separate checkpoint/model placement from Lightning's input datamodule/prediction loop. The measured `_PredictionLoop._on_predict_start` boundary already contains a request's loader; directly freezing that point would reuse request-bound data. Keep the accepted bounded-memory runtime and all original design stages. |

These timings exclude request-specific compilation and are not disk-cold
measurements. Native results and ranges are in
[the current startup report](../../../scientific-startup/current/current-h100-20260907.md).
The live ESM qualification applies the same separation: immutable weights are
captured before any input; ordinary controller-issued argument/localization
checks and complete scientific outputs run afterward.

RFdiffusion is the clearest next small worker bridge. Proteina needs explicit
pipeline/LoRA binding; BoltzGen needs careful separation of Lightning model and
datamodule state. None is declared CUDA-incompatible merely because its adapter
is not finished. Their roughly 30–36 s initialized startup makes a real optional
speedup plausible, but a storage-cold full-process image may still be slower.
Only matched native/restore trials can decide the default.

Implementation sources: model-owned
`runtime-images/rfdiffusion/runtime_entrypoint.py`,
`runtime-images/proteina-complexa/runtime_entrypoint.py`, and the accepted
`scientific-startup/current/boltz_sitecustomize.py` timing hook. No source,
runtime parameters, production policy or GPU capacity changed for this assessment.
