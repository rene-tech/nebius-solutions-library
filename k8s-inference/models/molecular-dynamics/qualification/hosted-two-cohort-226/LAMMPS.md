# LAMMPS: two complete canonical hosted cohorts

Both frozen fixture-04 workflows completed on the original Helm226 runtime.
The native worker image is
`e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c`.
Input archive SHA256 is
`cb0ab63bd6311acf030288db41544d7ea3988e47b7155da2dd62f18808162b53`;
request SHA256 is
`1d5b8f866487c6ff30a396f9753f67bb85cecb3b9b0626bfae30772f777cb091`.
No engine or scientific input changed between cohorts.

| Cohort | Operation | Verified artifacts | Native production ns/day |
|---|---|---:|---:|
| Primary | `46ea947b-fecc-4036-9c87-42df6886f1a6` | 40 | 28.470501 |
| Repeat | `445903bd-4e95-4483-a178-4c395b9a24b6` | 40 | 28.262356 |

Each completes minimization, 50,000 NVT steps, 50,000 NPT steps and 500,000
production steps, with 101/101/1001 unique native frames including stage starts.
The common analysis uses the actual 1–1000 ps production samples. The repeat's
performance is 86,400/(1801.79+1255.28) ns/day from its two printed native loop
times for 294,000+206,000 production steps; it is not hosted end-to-end speed.

The first strict duplicate-coordinate boundary check failed. Exact-source,
image/topology/file-bound analysis independently reconstructs LAMMPS's setup
SHAKE projection instead of relaxing the generic boundary tolerance. Both
native boundary records and the original failure are retained. The repeat's
different boundary step is independently validated. See
[the source-backed boundary report](../../comparison/analysis/LAMMPS-BOUNDARY-REPORT-20260923.md).
This proves native file continuation, not GPU-process restore or identical RNG state.

Authoritative native validation receipts:

- `/home/tux/fs2-alanine-lammps-20260923/hosted-primary-validation-02.json`,
  SHA256 `2ec4224a549ea288fccf95c8ea45aa70434f3f809cfe0adaba853b4a7145d56d`.
- `/home/tux/fs2-alanine-lammps-20260923/hosted-repeat-validation-01.json`,
  SHA256 `9e708227971d593d376e0d8b330cb07a32c6ad54769ede2b8f641c1a41e86123`.

The complete files and copied validation receipts are in the final delivery
bundle under `runs/lammps/` and `validation/`. The primary's native temperature,
pressure and density means are 299.823145 K, −1.495605 bar and 0.984620495 g/cm³.
The repeat's native temperature/pressure means are 300.079881 K and 0.681935 bar.
Identical input seeds make these operational repeats, not independent sampling
or a 1 ns convergence proof.

The original long result transfers exposed API debug-buffer OOMs. The read-only
final-client recovery repeats on Helm227 verified both complete result sets,
1,906,805,811 artifact bytes plus two manifests, without new simulations or
increased memory limits. That transport repair is separate from the original
scientific run evidence, not a relabeled Helm227 simulation.
