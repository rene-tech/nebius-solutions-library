# Independent topology-audit review, 2026-09-23

Scope: read-only review of the parent-owned `comparison/audit_topologies.py`,
using the actual `master-01` and `conversion-02` as inputs. Negative tests operate
only on copied converted files. No canonical inputs, parent source or simulations
were modified. This is scientific-parameter validation, not dynamics evidence.

The unchanged conversion passes all existing checks. Six deliberately modified
copies also pass, demonstrating missing checks rather than proving that the
unchanged conversion is physically wrong. Parent owns fixes and final acceptance.

| Priority | Missing gate | Demonstrated counterexample | Recommended bounded fix |
|---|---|---|---|
| High | All native cross-type LJ interactions | `pair_coeff 2 9 5.0 9.0` is ignored; GROMACS `[nonbond_params] CT OW 1 0.9 20.0` also passes | Reject unsupported cross overrides/NBFIX for this narrow ff14SB fixture, or explicitly audit every pair and force implementation |
| High | Effective LAMMPS commands | Later `pair_modify mix geometric` and later changed `special_bonds` both pass | Parse effective commands or reject duplicate/unsupported commands; substring/first-match is insufficient |
| High | Bonded term multiplicity | Extra copy of a bond with updated native count passes, because the dictionary collapses repeated atom pairs | Compare counts/multisets or explicitly reject repeated bonds/angles; preserve multiplicity in energies |
| Medium | Full periodic lattice | Nonzero `xy` tilt passes unchanged length checks | Check all box vectors/angles; reject nonzero tilt for this orthorhombic fixture |
| High | Input identity binding | Above changes leave the old conversion manifest untouched, and the audit still publishes only manifest hashes | Verify every actual source/converted file against its manifest and record actual file hashes before/after audit |

Line references at reviewed version: GROMACS exceptions-only force inspection
39–49; bond/angle dictionaries 100–103; diagonal LAMMPS pair parsing 115–121;
lengths-only cell and mixing substring 123–126; LAMMPS bonded dictionaries
129–132; first `special_bonds` match 152–155; manifest-only receipt hashes
192–193. The entire torsional potential-curve comparison is useful and does
preserve summed torsion terms; it does not cover ignored regular cross-type LJ.

Reproduction (28 analysis tests at initial commit, 30 after pressure hardening;
these six independent negative tests currently **fail**):

```bash
/home/tux/.venvs/fs2-four-engine-20260923/bin/python \
  comparison/analysis/tests/probe_topology_audit.py \
  --audit /absolute/parent/comparison/audit_topologies.py \
  --master /home/tux/fs2-alanine-comparison-20260923/master-01 \
  --converted /home/tux/fs2-alanine-comparison-20260923/conversion-02 \
  --output /new/evidence/directory
```

Observed receipt: `/home/tux/fs2-alanine-analysis-20260923/topology-review-01/receipt.json`.
Each case has a copied fixture and `negative-test-receipt.json` with its hashes.
`source_inputs_unchanged=true`; all six cases have `rejected=false` and
`observed_status=passed`. Keep this rejected cohort after repairs. The same
negative runner returns success only when baseline passes and every changed
fixture is rejected. No broad force-field-conversion qualification follows from
either the baseline pass or the analysis unit tests.
