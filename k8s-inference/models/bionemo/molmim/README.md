# MolMIM retained-checkpoint runtime

This independently implemented runtime uses the retained NVIDIA MolMIM 70M
checkpoint. It is not the NVIDIA NIM package, and numerical parity with NIM is
not claimed. Model licensing remains the existing NVIDIA AI Foundations Model
Community License/R&D-only policy.

## September 18 qualification repair

The previous port had three concrete defects:

- It omitted the outer residual around each Perceiver cross/self-attention
  block. The retained checkpoint was trained with that residual.
- Its tokenizer omitted NeMo's single-character fallback, rejecting valid
  single-backslash stereobonds such as the campaign's 3ERT/OHT input.
- The advertised CMA-ES branch was fixed Gaussian sampling followed by QED
  sorting, not an adaptive optimizer. If no candidate passed, it reported the
  input as a generated molecule and could silently underfill the request.

The repaired graph retains the checkpoint's tanh-approximation fused GELU and
outer residual, then greedily decodes complete sequences from CMA-ES latent
populations. There is no input substitution or unfinished-sequence acceptance.

Primary implementation references:

- BioNeMo source pin `9ba9b2c337917fc815ffbd5d99f78d9db7904493`:
  [controlled generation](https://github.com/NVIDIA/bionemo-framework/blob/9ba9b2c337917fc815ffbd5d99f78d9db7904493/bionemo/triton/controlled_generations.py)
  and [molecular tokenization/decoding](https://github.com/NVIDIA/bionemo-framework/blob/9ba9b2c337917fc815ffbd5d99f78d9db7904493/bionemo/model/molecule/infer.py).
- The checkpoint records NeMo 1.22.0. Its
  [Perceiver block](https://github.com/NVIDIA/NeMo/blob/v1.22.0/nemo/collections/nlp/modules/common/megatron/megatron_perceiver_encoders.py)
  adds the block-level residual;
  [fused GELU](https://github.com/NVIDIA/NeMo/blob/v1.22.0/nemo/collections/nlp/modules/common/megatron/fused_bias_gelu.py)
  uses the tanh approximation; the
  [regex tokenizer](https://github.com/NVIDIA/NeMo/blob/v1.22.0/nemo/collections/common/tokenizers/regex_tokenizer.py)
  appends the single-character fallback.
- Actual optimizer: [pycma 4.4.0](https://github.com/CMA-ES/pycma/tree/r4.4.0),
  `CMAEvolutionStrategy.ask`/`tell` per iteration, including covariance and
  step-size adaptation. The only suppressed optional-library warning is that
  pycma plotting is unavailable without matplotlib; this service never plots.

Checkpoint `.nemo` SHA256:
`10522c9db6018c355313f9f01a0edea2b021ddc0a5a22ae4540cbf5bdafbd1f5`.
Inner state dictionary SHA256:
`bc246330d019b18a4f0acaa88b65d64eaf6e5e940f1ec82e1ba097aff1fdddee`.

## Exact supported semantics

The API currently supports CMA-ES and QED only. `particles` is population size
(2–32); `iterations` is the number of ask/tell updates (1–16). The budget is
exactly their product. `num_molecules` (1–16) cannot exceed that budget.
`radius` multiplies upstream's initial sigma 0.75; it is not a guaranteed
molecular-distance radius. The random stream is local and deterministic from
the canonical input, without changing process-global NumPy RNG state.

The optimizer minimizes the negative sum of clipped relative Morgan/Tanimoto
similarity and directed QED/0.9, as in the pinned BioNeMo example. Unlike a soft
optimization objective, the advertised `min_similarity` is also enforced as
a hard final-output filter. Final candidates are distinct, valid, different
from the input, model-decoded, and ordered by requested QED direction. These
requirements do **not** guarantee that enough feasible candidates exist or
that each has improved QED. Each response records source QED and actual counts.

When the fixed budget does not yield the requested quantity, HTTP422
`GENERATION_EXHAUSTED` reports actual invalid, duplicate, unchanged,
below-similarity and feasible counts. It never returns an underfilled success
or a copy of the input. A scientifically valid exhausted search is not a
successful requested generation; callers may choose different parameters in a
new operation. Existing limits are not automatically expanded. Invalid input
or unsupported tokenizer entries return HTTP422 `INVALID_MOLECULE`.

The positional window is 128 tokens. Decoder sequences without EOS in that
window are rejected rather than silently truncated. Eight requested candidates
under a high similarity threshold can exhaust even when all decodes are valid.
QED is a computational descriptor, not efficacy, safety or synthesizability.

### Measured request profiles and important limits

On the exact repaired candidate, two isolated H100 repetitions returned eight
distinct changed molecules for seven chemically varied seeds with explicit
`particles: 16`, `iterations: 8`, `num_molecules: 8`, `min_similarity: 0.3`,
`minimize: false`. These are measured examples, not replacement defaults or a
guarantee for other molecules. The service still honors each supplied budget
exactly. The unchanged two-particle/one-iteration default exhausts for aspirin.

The original OHT request at similarity 0.7 exhausted in both repetitions, and
several explicitly requested larger-budget/radius experiments also exhausted.
Small perturbations decoded the seed repeatedly; larger ones often produced
valid molecules below the hard threshold. This demonstrates **finite-search
exhaustion**, not proof that feasible chemistry does not exist. Relaxing the
similarity threshold in a separate experiment produced eight candidates; that
does not satisfy or retroactively pass the original stricter request.

The final hard filter is stricter than NVIDIA's
[documented soft similarity objective](https://build.nvidia.com/nvidia/molmim-generate).
The pinned upstream implementation uses CMA-ES with sigma 0.75 and a soft
similarity/QED score; this port additionally rejects unchanged, duplicate and
below-threshold final outputs. Its radius-to-sigma multiplier and union of
feasible candidates across iterations are documented port behavior, not a claim
of exact NVIDIA NIM output parity.

Requested score direction controls optimization and final ordering; it does not
guarantee improvement over the seed. In the H100 minimize-QED tests, all eight
imatinib outputs had higher QED than the seed; caffeine improved only six of eight.
Seven of eight tested input molecules reconstructed exactly at zero perturbation;
imatinib did not. Decoder reconstruction and complete scientific fidelity are
therefore not guaranteed. See [the complete bounded evidence](QUALIFICATION-20260918.md).

## Verification and release boundary

`test_server.py` covers block recurrence, backslash tokenization, full-window
termination, real optimizer adaptation, quantity/similarity filtering,
minimize/maximize sorting, request-local RNG and actionable HTTP errors.
Set `MOLMIM_TEST_EXTRACTED_CHECKPOINT` to the retained extracted archive to run
the optional hash-verified CPU reconstruction check. It reconstructs aspirin
and caffeine; the old graph produced unrelated structures for both.

CPU regression or a direct isolated GPU response does not qualify public
HTTP/MCP or the customer workflow. The generated request-schema correction
(particles minimum and documented budget) is prepared with the qualification
record. The parent release owner must deploy it, pin the tested image, and
rerun affected customer-shaped tests. Until then,
production remains the historical image, not this repaired candidate.
