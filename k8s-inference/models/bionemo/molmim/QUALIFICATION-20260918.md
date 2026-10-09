# MolMIM candidate: bounded September 18 evidence

This is a repaired **candidate**, not a declaration that MolMIM is customer
qualified. Production model/catalog promotion is owned by the parent release.

Source `86ef557213243d1f110f9100b50365dcc3dd6024` fixes the retained-checkpoint
graph/tokenizer and replaces the misleading sampler with actual pycma 4.4.0.
See [README.md](README.md) for pinned primary sources and supported semantics.
Twelve focused tests pass, including real hash-verified checkpoint CPU
reconstruction; Ruff passes. The old graph reconstructed aspirin and caffeine
as unrelated structures; the repaired graph reconstructs both exact strings.
This small reconstruction sample does not prove numerical parity with NIM.

Published immutable candidate:

```text
cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/molmim@sha256:9df0b25cd79c86746f564ca060328811cd39792ce9e6d66ff9f3e1de6547a4cb
```

Linux-amd64 manifest: `0699011d800bfe61c9968b9c5e86db1d02e33e345826f550032fcadc4613346f`.
Config: `83f8068ae582870418ac1916f41344326feb1bd498352035ef59396d811697e4`.
Buildx provenance/SBOM attestation: `4702047dbec432d0660b49ea572d397b45e2c7c8dbb14628b4dc3b413c27e17e`.
Build used exact Git source, a distinct immutable tag, explicit `sandbox2`
registry authentication and private Docker config; no CLI-default change.

## Preliminary isolated GPU observations

The parent assigned an idle NVIDIA L40S while H100 slots served the existing
campaign. This is **L40S direct HTTP evidence, not H100 or public HTTP/MCP
qualification**. One task-owned pod ran on an existing node; no shared service,
quota, pool, node, model definition or production image was changed. Its
checkpoint cache was the image's existing immutable content. The pod and
localhost port-forward were removed after receipts were captured.

Two repetitions of eight cases produced 16 honest protocol/output results.
Only **4 of 12 valid generation requests succeeded**; eight explicitly
exhausted. Four additional malformed/impossible-input requests returned 422.
Do not count exhaustion as a successful requested generation.

| Case | Both repetitions |
| --- | --- |
| Imatinib, similarity 0.3, n=8, population 8, iterations 4 | Eight valid, distinct, novel molecules; about 1.2s |
| OHT, similarity 0.7, n=8, population 8, iterations 4 | Explicit exhaustion; no feasible output |
| OHT same budget, radius 0.25 | Explicit exhaustion; decodes remained input |
| Estrogen, minimize QED, similarity 0.3, n=8, population 8, iterations 4 | Explicit exhaustion; six feasible candidates |
| Estrogen same request, iterations 16 (existing bound) | Eight valid, distinct, novel molecules; 4.5–4.8s |
| Aspirin default population 2 / iteration 1 | Explicit exhaustion; no seed substitution |
| Invalid molecule / impossible decode budget | Explicit 422 rejection |

Every 200 response was independently parsed with RDKit and checked for exact
requested quantity, canonical uniqueness, difference from the input,
recomputed QED and Morgan/Tanimoto similarity, and requested score ordering.
No underfilled success or input fallback occurred. Restrictive requests and
the very small unchanged default budget remain real usability limitations.
Limits were not automatically enlarged to make these cases pass.

Private receipts, model outputs, exact commands and resource identities:
`/home/tux/secure-handoff/molmim-cmaes-candidate-20260918-LN4bKT/`.
`l40s-http/summary.json` SHA256:
`b02706fea490d5865bb5589bba43f03aa73c2e6e5f79276bda6e682120283199`.
`cpu-reconstruction.json` SHA256:
`3f849658f47ac78a3a89a9acdabb07091d3fb6e8e82cfe20275b7b37a2cb5d62`.

## Exact-candidate H100 follow-up

The unchanged image `9df0b25c...` ran on one existing preemptible H100 80GB HBM3,
driver 580.159.04, GPU `GPU-b9790cfd-34f8-8c04-faea-56b5cedc79d7`.
Node `computeinstance-e00rdppqt3kg2y00gj`, UID
`c36d3b47-6536-4feb-aa5e-1b18bcdd4ccf`; task pod
`fs2-molmim-h100-qualification-20260918`, UID
`7bec1aa9-fa79-4eac-837c-24921c21224b`. No production image, model settings,
quota, pool, capacity or admission limit changed.

Between 19:35:13 and 19:38:24 UTC on September 18, 29 explicit requests were
repeated twice. All 58 response/budget checks passed, but **only 36 requested
generations succeeded**, returning 260 molecules. Eighteen searches exhausted
with explicit 422 and four malformed/impossible-budget requests were rejected.
Both repetitions returned identical bodies, consistent with the canonical-input
seed contract. No numeric RNG-seed API is advertised.

| Observed request | Result in both repetitions |
| --- | --- |
| Original imatinib, n8/p8/i4/similarity0.3/max | Eight distinct valid changed molecules; about 0.93–0.96s |
| Original OHT, n8/p8/i4/similarity0.7/max | Exhausted: 31 below similarity, one unchanged, zero feasible |
| Original estrogen, n8/p8/i4/similarity0.3/min | Exhausted: six distinct feasible of eight requested |
| Seven varied seeds, n8/p16/i8/similarity0.3/max | Every request returned eight, all with higher QED; about 2.34–2.95s |
| The same seven seeds, n8/p32/i16/similarity0.3/min | Every request returned eight; improvement is not universal |
| OHT n8/p32/i16/similarity0.7, radius0.1 or0.25 | All 512 decodes unchanged; explicit exhaustion |
| OHT same request radius0.5 / radius2 | Zero feasible; respectively 234 unchanged+278 below threshold /512 below threshold |
| OHT new request with similarity0.3, n8/p32/i16/max | Eight outputs; not a pass of the original similarity0.7 request |
| Unchanged p2/i1 defaults | Aspirin exhausted; caffeine and ibuprofen each returned one changed molecule |

The seven seeds are imatinib, estrogen, aspirin, caffeine, ibuprofen, erlotinib,
and gefitinib. Every success was checked independently for RDKit validity,
canonical uniqueness and difference from the input, exact quantity, recomputed
QED/Morgan similarity, hard similarity bound, requested sorting direction,
exact `particles * iterations` decodes and mutually exclusive outcome counts.
For minimize-QED, imatinib improved zero of eight and caffeine six of eight.
This is real guided search, not a guarantee of an improved property.

An additional **internal diagnostic**, not an HTTP capability, substituted three
numeric RNG seeds (11/29/47) while retaining the same image graph, optimizer,
objective and explicit budgets. Original OHT still had zero feasible candidates;
p32/i16 yielded four/five/five, all below the requested eight. Original estrogen
yielded nine/seven/nine feasible candidates. Recorded step size, mean and
covariance condition changed over iterations. This supports finite-budget,
seed-sensitive search exhaustion; it does not prove chemical infeasibility.

Zero-perturbation reconstruction was exact for seven of eight input molecules.
Imatinib reconstructed a different valid structure (Morgan similarity 0.3684),
so this broader probe does not establish complete reconstruction fidelity or
NIM numerical parity. No additional source defect was demonstrated by this
bounded follow-up; the runtime and image stayed frozen.

## Upstream comparison and reproducibility

The pinned BioNeMo source uses `bionemo-controlled-generation==0.3.0`. Its
[official wheel](https://pypi.org/project/bionemo-controlled-generation/0.3.0/)
SHA256 is `c7c9bdca6d5026eacbde5da3f88c8250c418b3bbf97c64c91f3fbfec611f3c94`.
Read-only inspection confirms ordinary CMA-ES ask/tell and 2048-bit,
radius-two Morgan/Tanimoto scoring. The pinned endpoint's similarity/QED oracle
is soft; its final selection is from the last population. The port's hard final
filter, all-iteration distinct-candidate collection and radius multiplier are
explicit differences. The current [NVIDIA model page](https://build.nvidia.com/nvidia/molmim-generate)
also describes similarity as a soft constraint. Its
[optimization tutorial](https://docs.nvidia.com/bionemo-framework/1.10/notebooks/cma_es_guided_molecular_optimization_molmim.html)
explores population, iteration and sigma sensitivity rather than promising
success for every fixed search budget. None of those examples authorizes
silently enlarging a caller's budget here.

The frozen request matrix and reusable checker are
`qualification-20260918-cases.json` and `qualify_http.py`. To repeat on an
explicitly authorized isolated H100 endpoint with RDKit/httpx available:

```sh
python qualify_http.py --base-url http://127.0.0.1:PORT \
  --cases qualification-20260918-cases.json --output PRIVATE_NEW_DIRECTORY
```

Private H100 root:
`/home/tux/secure-handoff/molmim-h100-qualification-20260918-8XK7nd`.
`http/summary.json` SHA256:
`1230ddb5fead2eeb959e21bd95e7b118f311e6b6553d2aa030165e07f6c5b49b`.
`diagnostic.jsonl` SHA256:
`eaa8ad43630257ecbf033ab83c5f65f9601c67f6a82d483467dbbd581388218a`.
`identity.json` SHA256:
`988a5844c663399f729b9a61a6ec2783f2c4588f0aa6a5bada19a246be6ed586`.
The task pod was deleted and verified absent at 19:39:41 UTC; its local
port-forward exited. `cleanup.json` and final pod/log receipts are retained.

The original twelve runtime unit tests remain source-bound evidence; no runtime
source changed in this follow-up. A fresh host test invocation could not collect
because that lightweight HTTP-checker virtual environment lacks torch; the
candidate image also intentionally has no pytest. Those tooling checks are not
reported as new passing runtime tests or model failures.

The tracked checker was subsequently replayed against all 58 retained responses,
with exact request matching and no new network/GPU work; all checks passed. The
canonical two-request semantic-validator fixture selects two genuinely passing
requests from this larger matrix, not the known-exhausting default example.

## Remaining promotion boundary

The candidate can be considered for the documented bounded generation contract,
including truthful finite-search exhaustion, not guaranteed eight-candidate
yield for strict similarity or guaranteed property improvement. A proposed
successor qualification record does not select a live runtime. The prepared CP
schema corrects the particle minimum from one to two without raising any limit
or changing defaults, and its descriptions explain the fixed budget, hard
filter, radius-to-sigma relationship and exhaustion. The parent must deploy
that source, review the complete current contract after the other molecular promotions, then
qualify public HTTP/MCP against the exact promoted image. Historical cold-start,
snapshot and elasticity receipts are not transferred to this digest.
No computational descriptor establishes efficacy, synthesizability or
experimental validation.
