# BoltzGen public protocol repair — 19 September 2026

Release174 exercised both repaired paths through ordinary scoped scientist09
public requests, not a direct Pod substitute. Runtime image remains
`sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e`;
recipe `7f158d0081c8b7541ed169e543ab37934504d443b6a1dfd2115734c0716bb62e`;
execution identity
`d07eea4259898300c5751643ab82f09d65d8eace03e227874a71596875aad50b`.
Gateway image was
`sha256:4a82bc67102509efa65a88b6eed449244dcc736c1100e8862dbaf7a8f6f11d58`.
The original input archives, counts, budgets, weights, diffusion settings,
resources,64MiB shared memory and filters were unchanged.

| Protocol | Public operation | End-to-end seconds | Result |
| --- | --- | ---: | --- |
| Antibody-anything | `567c2d79-1757-46e5-b228-f2a7cc95f49b` |873.15|All stages and returned artifacts complete; original evaluator false failure retained, separate source-backed reassessment below|
| Protein-redesign | `5d8fdda5-3eb0-4d54-9179-345208829b38` |641.44|Single-chain result passes unchanged exact sequence, length, geometry and fixed-region RMSD limit; measured0.75520Angstrom versus1.5Angstrom limit|

The preceding immutable `protocol-repair-20260919.json` records the original
antibody shared-memory bus error and redesign collector rejection, isolated
H100 repair evidence and cleanup. Those historical failures remain failed.
The successor uses supported `num_workers=0` and protocol-specific collection;
other binder protocols retain interface requirements.

## Antibody evaluator correction, not rewritten evidence

The original public evaluation required a full fixed framework but used only
residues with C-alpha coordinates. Returned heavy-chain atoms omit one fixed
Asp, while the mmCIF `_entity_poly_seq` and CSV both retain that Asp. The source
scaffold already lacks that interior coordinate: source chainA label62 maps to
output chainB label64 after allowed CDR insertion lengths. Its fixed
`TYYADSVKGR` sequence context is unchanged. This is not an inferred or fabricated
coordinate.

Pinned upstream `HannesStark/boltzgen`
`31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`,
`src/boltzgen/data/write/mmcif.py` lines104–105 emits full sequences including
missing residues; line137 skips absent coordinates. Workbench source`2b679d6`
therefore validates emitted full polymer sequences against every observed
`label_seq`/residue pair before using them for sequence constraints. Geometry
still uses actual coordinates. Missing positions are explicit, metadata
contradictions fail, and distinct frameworks require distinct output chains.
50 focused/evaluator tests and Ruff pass, including real fixed mutations and
missing-coordinate/contradictory-metadata regressions.

Original evaluation SHA256
`9837989dfc7e5ff978fd194802422edb4881f3e0bba5690749e58278c5257faf`
is retained. Separate reassessment SHA256
`e3e153569b78c4d26535e972c6167e2ec1ae10158104b7748ef0d7bb9763101e`
passes the **unchanged** frozen constraints with heavy/light chainsB/C and
missing heavy coordinate64 explicitly recorded. No new inference was used for
reassessment.

## Evidence and remaining boundary

- Exact public manifest SHA256:
  `7e57f574c2762f95aaaa2c3d62217c18fd2e710d54df66e4bd5cac6ec9bd9300`.
- Protected successor summary SHA256:
  `c88d4e32e1ebbd215ed3e36d5578c0663c25baaed83a13f3a4386c351a31c252`.
  It binds both requests, returned results, original evaluations and reassessment.
- Redesign evaluation SHA256:
  `494f17c9c53e544d43a2793583914c6afba44f5a4c34a6fbb14f7968dcd0947e`.
- Both operations terminal and scientist09 released. Earlier task-owned isolated
  resources were cleaned. Two final admin-detail reads returned503 and remain
  unavailable in this summary; do not infer final stage usage from wall time.

This is bounded public pipeline/sequence/geometry evidence, not binding affinity,
all-atom completeness, antibody developability, biological efficacy or clinical
qualification. Preserved missing coordinates require downstream review.
Elapsed time is not measured device utilization. Existing peptide, nanobody,
small-molecule and RFdiffusion motif evidence belongs to its own earlier exact
identities; it is not silently relabeled release174 acceptance. Final natural
scientist chat and unchanged-release customer-cohort gates remain separate.
