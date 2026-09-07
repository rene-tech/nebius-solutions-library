# Proteina-Complexa and BoltzGen model-only snapshot assessment

Normal loading remains the correct default for both current runtimes. Neither
accepted one-shot process exposes a reusable GPU-resident point before request
state is attached, so no GPU capture was started. Capturing the existing
"initialized" markers could produce a fast-looking restore while silently
reusing a target, seed, DataLoader, writer or output workspace.

## Proteina-Complexa

The exact H100 runtime is image
`sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca`
at upstream revision `54058860d43444c7289873f77d3e50b5b02348cd`.
Three current cached-image/weight, fresh-process runs reached the native
`cfg_gen` marker in a median 30.263 seconds (29.961–30.359).

The upstream source timestamps further isolate the model-loading interval:

| Repetition | checkpoint-load start → `cfg_gen` |
|---|---:|
| 1 | 8.640 s |
| 2 | 8.739 s |
| 3 | 8.239 s |

That 8.640-second median includes checkpoint and partial-autoencoder loading,
optional LoRA recreation/reload, and `configure_inference`. It is only 28.5%
of the initialized clock, 16.0% of the 54.12-second native generation median,
and 3.5% of the 244.666-second validated public-operation median.

More importantly, this is not a GPU-ready boundary. The model is loaded on the
CPU. The request's Hydra generation configuration is already bound, then the
target and DataLoader are instantiated, and only `Trainer.predict` moves the
model to CUDA. A capture after that move would already contain request-specific
target, seed, DataLoader and output state. The public model also represents
three distinct checkpoint pairs; ligand and AME reapply LoRA, while AME selects
a different architecture. A single reusable bundle cannot truthfully cover
them.

A future version can still be useful, but it is a serving refactor rather than
a small snapshot hook: one persistent worker per exact checkpoint/architecture/
LoRA variant should load and place only immutable model state on CUDA before it
accepts requests, then rebuild the original Hydra configuration, target,
DataLoader, seed and output validation for every request. Each supported
variant needs two original-input qualifications before selection.

## BoltzGen

The exact H100 runtime is image
`sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e`
at upstream revision `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`.
Three fresh-process design-stage trials reached the CUDA-synchronized
post-restore prediction-loop marker in a median 36.052 seconds
(33.897–36.160). Complete validated 20-candidate design output took a median
288.319 seconds, so the entire initialized window is 12.5% of that stage. It is
2.9% of the current 1,239.974-second validated public multistage operation.

The measured marker is deliberately after
`_PredictionLoop._on_predict_start`, but by then `Trainer.predict` has already
received the request-specific DataModule. The surrounding `Predict` object
also retains the request's writer, output path and sampling arguments. Directly
freezing that process would freeze input state.

BoltzGen also swaps from the first design checkpoint to the second checkpoint
halfway through the accepted 20-candidate design run. A startup image of the
first model does not eliminate the later load. The production route then runs
design, inverse-folding, folding, design-folding and optional affinity in
separate processes, with different models and request-derived handoffs. One
model-only snapshot cannot cover that pipeline.

The viable successor is therefore a set of explicit stage workers, not a
capture of the current one-shot Job. Each worker must preload one immutable
model identity before accepting work, reconstruct the original stage
DataModule/writer/workspace after restore, preserve the mid-run design
checkpoint switch, and pass two original inputs plus the complete bounded
multistage validator. The existing analysis settings remain exactly
`num_processes=1`, `data.cfg.num_workers=0`, and `pin_memory=false`.

## Evidence and scope

Machine-readable identities, clocks, source hashes and the exact successor
requirements are in
[`proteina-boltzgen-model-only-assessment-20260907.json`](proteina-boltzgen-model-only-assessment-20260907.json).
The underlying measurements remain in the current Proteina and BoltzGen
startup reports and the current public scientific regression receipts. This
assessment did not create a Pod or GPU run, modify production, change an
algorithm, or raise any resource limit.
