# Lynx workload coverage: actual requests versus representative tests

As of 3 October 2026. This is a benchmark plan, not a claim that a complete
customer production pipeline passed. Use existing **system/qa** only. Do not use
customer keys, change their instances, copy private structures/prompts into Git,
or silently replace an original input with a public benchmark. Preserve prior
failed and incomplete chats, native failures, exact images and input hashes.

## Actual customer evidence

| Recorded request | Authoritative evidence | Reusable input/path and acceptance |
|---|---|---|
| Inventory an uploaded CIF, without a preparation study | [30 September request](https://nebius.slack.com/archives/C0C3MAREU3D/p1790751815760129), [reported 120-minute failure](https://nebius.slack.com/archives/C0C3MAREU3D/p1790762952196709) | Private, hash-pinned original CIF and `cif-inventory` prompt. Compare agent answer to `inspect-mmcif.py`: chain/residue/atom/water facts; no invented receptor-loop or atom-completeness claims. CPU only. |
| Recommend a small-molecule force field for GROMACS | [1 October original question](https://nebius.slack.com/archives/C0C3MAREU3D/p1790809742846649) | Private `forcefield-recommendation` prompt. Advice-only: compatibility caveat and concrete next step, no unsolicited parameterization or MD. |
| Actually parameterize 20-hydroxyecdysone using OpenFF Sage | [Direct execution request and transcript](https://nebius.slack.com/archives/C0C3MAREU3D/p1790830997262329) | Private `openff-parameterization` prompt preserves the supplied stereospecific molecule. Installed CPU helper must finish once, deliver GROMACS ligand files and verified charge/version/hash provenance; loading a skill is not completion. |
| Keep the LynxKite visual pipeline/settings, execute GROMACS without an LLM, delegate GPU management | [Direct API request](https://nebius.slack.com/archives/C0C3MAREU3D/p1790763065473489), [pipeline/priorities clarification](https://nebius.slack.com/archives/C0C3MAREU3D/p1790767220638959), [provided API/MCP example](https://nebius.slack.com/archives/C0C3MAREU3D/p1790850306957369) | REST and programmatic MCP are actual requested paths. The supplied example is a provider-authored prepared-TPR recipe, **not a customer-supplied scientific protocol**. Exact customer pipeline comparison remains blocked on its prepared native bundle/settings. |
| Estimate GROMACS work at different GPU sizes | [30 September suggestion](https://nebius.slack.com/archives/C0C3MAREU3D/p1790767267418109) | Explicitly proposed as a later feature. Current 1/2/4/8/16-GPU tests may inform it, but the specific scaling matrix and MEM fixture are our assumptions, not their requested protocol or a validated estimator. |

The channel mentions prospective MD/FEP work, but does not supply a complete
GPCR/membrane system, topology, lambda schedule, equilibration protocol or
production TPR. Do not invent those or present alanine/MEM as their actual study.

The authoritative client checkout is `rene-tech/serverless-ai-cookbook`,
`templates/hcls-librechat`. Its
`docs/lynx-instruction-replay-20261001.md`,
`docs/general-openff-release-20261001.md` and
`docs/ripple-lynx-evaluation-20261001.md` retain prior evidence and failures.
Earlier real OpenFF outputs passed identity, 78-atom count, neutral charge and
hash checks; that does not prove receptor assembly, GROMACS energy equivalence,
force-field accuracy, or completion on the current image. The earlier hosted
alanine example was 20 ps NVT + 20 ps NPT + 20 ps production, not 1 ns NVT.

## Explicitly assumed representative cases

| Case | Existing fixture/command | What it tests, and what it does not |
|---|---|---|
| Public structure inventory | `default-release-cases.json`: `structure-inventory`, public 1UBQ CIF | Reusable nonprivate CPU inspection control; not the original customer CIF. |
| Neutral and charged ligand preparation | `openff-aspirin`, `openff-cation` | Installed Sage 2.2.1 + AmberTools AM1-BCC; canonical identity, formal charge, exported topology/coordinates, hashes and working download links. Not MD or accuracy validation. |
| Undefined stereochemistry and missing input | `undefined-stereochemistry`, `missing-file` | Ask/report the real missing choice/input; no guessed isomer, substitute structure, package install, or speculative GPU job. |
| CHARMM-family compatibility advice | `force-field-advice` | Advice-only regression against unsupported automatic compatibility claims; this protein force-field choice is ours, not a Lynx-supplied protocol. |
| Native preparation → equilibration → analysis | `hosted-default-cases.json`: `hosted-gromacs`; `/workspace/examples/v3/molecular-dynamics/alanine-quickstart/gromacs/` | Unchanged packaged alanine workflow via actual seeded agent/installed skill/MCP; also direct API helper control. Compare reports to native files. One GPU, independently coordinated with the active QA policy owner. |
| Prepared-TPR performance and output retention | Existing MPINAT MEM, RIB, PEP, PEP-h fixtures and `prepare.parameters` | Three unchanged-input 10,000-step repeats, warmup-inclusive timing, actual GPU flavor/allocation, useful work and full output delivery. They are representative systems, not Lynx inputs or independent scientific samples. |
| Strong scaling / continuation / concurrent work | Existing MPI matrix and native `.cpt` recovery fixtures | Separate baseline/shape/transport/control identities. Recovery follows the same operation/native checkpoint. Eight-/sixteen-GPU live evidence remains capacity-dependent; no borrowed customer capacity. |

## Ready-to-automate inputs

`lynx_workloads.py` is **offline preparation only**. It emits separate actual,
assumed-CPU and assumed-hosted manifests accepted by the existing client
`scripts/qualification/replay_agent_instructions.py`, plus a hashed dispatch
plan. Actual prompts are byte-for-byte text-preserved. Explicit original file
hashes are required, mixed assumed/customer manifests are rejected, output must
be fresh and outside Git, and private files have mode 0600. It accepts no key and
does not install, launch, submit, resize or change admission policy.

```sh
python lynx_workloads.py \
  --client-root /path/to/client/templates/hcls-librechat \
  --private-cases /private/lynx-cases.json \
  --private-cases-sha256 EXPECTED_ORIGINAL_CASES_SHA256 \
  --private-cif /private/original.cif \
  --private-cif-sha256 EXPECTED_ORIGINAL_CIF_SHA256 \
  --public-cif /retained/public/1UBQ.cif \
  --public-cif-sha256 EXPECTED_PUBLIC_CIF_SHA256 \
  --output /private/fresh-lynx-replay-plan
```

Omit all four private-input arguments for assumed controls only. Retain the
original CIF in a read-only local QA mount at the exact path in the private
dispatch plan; do not publish it to a customer or shared system bucket. Reuse
the existing isolated QA client and its actual seeded agent; qualify with
`--use-seeded-agent`, not an unrecorded alternative model/instruction set.
Start with the actual and assumed CPU-only files. Run the hosted manifest only
after the existing matrix policy owner explicitly has a slot; never run two
competing QA admission-policy loops. Actual API, programmatic MCP and agent/MCP
results remain distinct evidence categories.

The public-equivalent inventory control uses unmodified RCSB
[`1UBQ.cif`](https://files.rcsb.org/download/1UBQ.cif), retained with SHA-256
`056f98710cb2b36f633c45e41902a02eb446e82871da21ff2dd44f74a56ca0f6`
(103,220 bytes), mounted read-only at `/workspace/inputs/1UBQ.cif`.
`--public-cif` records and validates that binding independently of the original
private CIF. The other five CPU controls are self-contained text/SMILES or a
deliberately missing path; no public molecular structure is silently treated as
the original customer input. The hosted alanine fixture is already packaged in
the workbench example path above; preserve its native parameters unchanged.

Installed deterministic controls, inside that isolated client:

```sh
/opt/openff/bin/python /opt/openff-release/verify-runtime.py
/opt/scientific-client/bin/python /opt/bionemo/inspect-mmcif.py PRIVATE_CIF --format json
/opt/openff/bin/python /opt/bionemo/prepare-openff.py \
  --inspect-identity --smiles 'EXACT_PRIVATE_INPUT'
/opt/openff/bin/python /opt/bionemo/prepare-openff.py \
  --smiles 'EXACT_PRIVATE_INPUT' --force-field openff-2.2.1.offxml \
  --output /workspace/replays/FRESH_ID/ligand
```

Check actual installed helper/interpreter paths first. The OpenFF helper is
SMILES-only ligand preparation: its padded empty export box is not solvation,
and its finite CPU energy is not GROMACS cross-engine validation. Dependencies
belong in the pinned release image, not an interactive customer install; see
[OpenFF installation](https://docs.openforcefield.org/en/latest/install.html) and
[Interchange export limitations](https://docs.openforcefield.org/projects/interchange/en/stable/using/edges.html).

Record CPU AM1-BCC seconds separately from chat/token waiting and GPU occupancy.
For every executed MD case retain operation/attempt IDs, exact input/recipe/image,
node/GPU identity, native commands, three timing records, checkpoints, downloaded
files/hashes and links. Treat incomplete replies and incorrect scientific prose
as failures even when a native operation succeeded. No current-release pass is
claimed by this prepared manifest.
