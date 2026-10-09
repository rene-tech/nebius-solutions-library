# ProteinMPNN incomplete experimental backbones

The current scientific cohort completed 180 inverse-folding requests. Eighteen
returned runtime HTTP 500: every seed/temperature for PDB 1QYS and 1TIM. Both the
reserved and burst pods reported `RuntimeError`; operation
`bbbfa818-49ed-464a-9a47-3d646c49666a` is one traceable example.

CPU-only parsing and featurization using the exact installed upstream code
established the reason:

- 1QYS has 91 observed residues across 92 numbered positions. Position 35 in the
  parsed chain is a coordinate gap; the upstream native tensor represents it as
  `X` with backbone mask zero.
- 1TIM has 247 observed residues across 248 numbered positions, with the gap at
  position 3.
- The upstream model preserves unknown/native residues at nondesignable
  positions. The adapter previously rejected every noncanonical character,
  including those legitimate X placeholders, in its final semantic gate.

`validated_chain_output` now accepts X only when the matching input/native
position is X **and** the backbone mask is zero. It rejects unexpected X at
resolved positions, invented substitutions at missing positions, unsupported
characters and length/mapping mismatches. No residue is silently removed,
renumbered or filled. Existing complete-backbone output remains unchanged.

Native responses with incomplete inputs include `backbone_coverage` with
per-chain, 1-based incomplete-backbone and unresolved-sequence positions, plus a
machine-readable `incomplete_backbone` warning. These positions are in the
returned chain including upstream numbering gaps; they are not author residue
numbers. Consumers must make an explicit gap policy before downstream refolding
and must not interpret X as a designed amino acid.

A separate test-harness bug affected nine 1TEN cases: its ARG802 has C/O atoms
but no CA. The independent evaluator previously used only CA-bearing residues
and incorrectly expected 89 positions. The supplied PDB explicitly contains 90
residues, and those nine actual outputs are valid. Their original failed
evaluations are retained alongside append-only correction receipts; no model
reruns were used to change their verdicts.

CPU evidence: eight native-adapter tests pass, including unexpected-X rejection,
unknown-position preservation, known partial residues and customer-visible
coverage metadata. This is not a deployment or GPU qualification receipt.
The campaign manager owns image promotion and rerunning all 18 failed cases,
with complete-backbone regressions and downstream handling checks.
