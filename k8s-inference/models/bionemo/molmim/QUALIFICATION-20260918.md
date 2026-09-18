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

Remaining gates: resolve/describe practical request defaults and constrained
search failures; update generated schema for population minimum and budget;
qualify the exact promoted image on H100 and customer-shaped public HTTP/MCP.
No computational-descriptor result establishes efficacy, synthesizability or
experimental validation.
