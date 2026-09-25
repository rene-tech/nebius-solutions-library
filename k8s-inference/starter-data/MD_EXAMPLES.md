# Molecular-dynamics starter pack v3

Deployed as the default in Helm revision 230 after the bucket canary passed.
Both sequential seeded-client cohorts and all-bucket verification passed; see the
[delivery record](../acceptance/customer-starter-data-md-20260925/README.md).
Never distribute a draft pack. This extends the qualified v2
data artifact and preserves its 120 cases and exact unchanged recipe/input bytes.
The category `molecular-dynamics` matches the live catalog. Five workflow examples
share **one** canonical molecule; this is not presented as five different systems.

## Included workflows

| Case | Apps | Native protocol |
| --- | --- | --- |
| `alanine-quickstart` | GROMACS, NAMD, AMBER, LAMMPS | Minimize, 20 ps NVT, 20 ps NPT, 20 ps production |
| `alanine-1ns` | All four | Minimize, 100 ps NVT, 100 ps NPT, 1 ns production |
| `alanine-restart` | All four | Continue the supplied completed 1 ns native state for 20 ps |
| `alanine-replicas` | All four | Two independently seeded introductory jobs, separate workspaces |
| `alanine-umbrella-window` | GROMACS | One phi=-180° window, k=200 kJ/mol/rad², 100+100 ps equilibration, 2 ns production |

There are 17 recipes. The umbrella example is a single-window tutorial, **not**
a global PMF or the complete 24-window study. The previously completed 48 ns
study remains a separate optional reference. Short workflows illustrate service
usage, not equilibration or converged populations. No engine binaries or GPU
snapshots are distributed; native restart is not CUDA checkpoint restore.

Canonical chemistry: ACE–ALA–NME, ff14SB, 2192 explicit TIP3P waters, 6598 atoms,
300 K, 1 bar, 2 fs, 1 ps coordinate output. Topology and source hashes are pinned
to the retained four-engine delivery. The builder refuses modified source files.
The lighter variants explicitly change duration or seeds, not force-field terms.

## Reproducible build

Use the qualified v2 data image's `/pack` directory as `BASE`. `DELIVERY` is the
frozen canonical comparison bundle with manifest
`e9df4cee5404587cbed77387e7abb52636720be37e10412e75e8d6718e40e01e`.
`CONTRACTS` contains customer-authorized `get_model_schema` responses for the four
Apps. `UMBRELLA` contains the original prepared window inputs and preparation
receipts. Binary inputs belong in the immutable data image, not Git.

```sh
python md_examples.py --base "$BASE" --delivery "$DELIVERY" \
  --contracts "$CONTRACTS" --umbrella "$UMBRELLA" --output "$NEW_DRAFT"
```

`md_acceptance.py` prepares an isolated disposable customer identity and runs
selected recipes using its public MCP key. No admin key submits simulations.
Run a fresh second cohort against the same final inputs; reusing an output folder
resumes its original operation and is not a new independent execution.

`md_analyze.py` verifies all downloaded object checksums, native command/stage
completion, every native frame and atom, finite coordinates and cells, requested
duration/cadence, peptide geometry, and density. It writes per-job phi/psi CSVs,
a plot and a machine-readable receipt. It never substitutes rendered or
interpolated frames for native data. No convergence or four-engine force equality
is inferred from these functional checks.

For the umbrella tutorial, native 0.1 ps phi/psi pull observations must also agree
with the angles calculated from the 1 ps saved coordinates, using periodic
angular differences. This does not claim a global PMF or independent compiled
bias validation. `md_validate_cohort.py` aggregates these exact-input proofs;
missing or failed recipes cannot qualify the pack. Acceptance executes the
checksum-verified **bundled** runner and analyzer, not a newer checkout version.

Native files are byte-verified before analysis. The runner streams large outputs
to temporary files and publishes only complete verified bytes; interrupted GETs
are retried without repeating model submissions. CLI exit 2 means still pending,
not success. Reusing a run directory resumes that operation; every fresh run or
different engine needs a separate directory.

## Packaging and release

- Reuse `qualify_pack.py` with exact input/recipe hashes and fresh MD validation
  receipts; inherited v2 proofs remain explicitly historical for unchanged data.
- The MD category explicitly requests five workflows, while previous categories
  retain their existing ten-case minimum. Every advertised recipe still needs
  evidence; lowering variety does not waive live execution.
- Keep the seed payload under the existing 128 MiB/32 MiB object bounds.
- New large MD results are streamed and checksummed. Per-recipe download budgets
  are explicit; the default for old recipes remains 128 MiB. This does not change
  cluster, tenant, API, storage or cloud quotas.
- Pin only the new data image and manifest through the current live chart. Do
  not redeploy this historical branch's control-plane image or chart templates.
- Canary first, verify actual seeded objects with bucket-scoped credentials,
  then let the existing create-only reconciler backfill eligible buckets.
- Keep old versions, customer changes, disabled users and excluded storage intact.

`release_seed.py` reconstructs the installed Helm chart from its release record,
checks a server-side dry run and rejects unrelated resource/image changes. It
recaptures each new live revision rather than overwriting parallel deployments.
`md_workspace.py` verifies actual installed objects using only the disposable
customer's API key and scoped storage credentials. `md_cleanup.py` retires only
the campaign's idle identities and retains buckets and evidence.

## Failures retained during development

An initial Amber batch used nine-digit seeds, and native output validation
reported overflowed fields. Candidate replica seeds now retain the original
eight-digit width and remain independent. Both corrected replica jobs completed
and passed native validation in operation
`0ff4892a-cc90-402a-b542-d1355b6db66d`; independent repeat
`c62ef226-238d-4d20-afa1-e39d022a40f7` also passed both replicas.
Native finite/overflow checks were not weakened to turn this failure into a pass.

That failure also exposed a separate shared collector defect: multiple failed
native diagnostic files reused one upload identity and caused HTTP409. The
original operation and Loki logs are retained. Fixing that frozen runtime
requires separate profile/recipe requalification; the starter data release does
not claim to fix or qualify every failure-diagnostic path. Follow-up:
`fs2-failed-native-diagnostic-upload-identity-r20260925`.

## Provenance and access

Generated example coordinates and authored scripts use Apache-2.0. Amber force
fields are public domain, as described by the authors in
[AmberTools](https://doi.org/10.1021/acs.jcim.3c01153). Attribute
[ff14SB](https://doi.org/10.1021/acs.jctc.5b00255) and
[TIP3P](https://doi.org/10.1063/1.445869). Engine licensing and ordinary key grants
continue to apply; having example files never grants execution rights.

Customer prompts are instructions to the existing agent, not proof of a newly
qualified browser release. This data update does not change LibreChat.
