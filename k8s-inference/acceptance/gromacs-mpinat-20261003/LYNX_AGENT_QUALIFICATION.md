# Lynx CPU workflows on the r4 client — 3 October 2026

These are three actual recorded customer requests and six separately labeled
representative controls, not a substitute for the customer's missing prepared
GROMACS protocol. The original private prompts/structure remain outside Git.
No customer credentials, customer bucket, customer instance or GPU work was used.

Exact client source: `a09a40c1a099bcb2e99c7e03caa644b30f78e86b`.
Image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:6d8b2038097b180d5edd997d7346a1b56c879a5f00b4890fcc08b81c2a96b2da`.
Actual seeded agent: Kimi-K3, high reasoning, unchanged 25-tool budget. Internal
identity: system/qa. At most two chats ran concurrently, one from each manifest.
The original private CIF and public 1UBQ control were hash-pinned read-only local
mounts; no input was copied to a shared bucket. The candidate retained separate
local `/data` and `/workspace`. There was no model/runtime overlay after launch.

## Observed results

| Evidence class | Case | Chat seconds | Tool calls | Result |
| --- | --- | ---: | ---: | --- |
| Actual | Original CIF inventory | 3.0 | 2 | Measured chain/residue/atom inventory, verified source link; no preparation study |
| Actual | Small-molecule force-field recommendation | 6.0 | 1 | Advice only, correct family compatibility caveat; no unsolicited execution |
| Actual | 20-hydroxyecdysone / OpenFF | 99.4 | 7 | Exact supplied stereochemical input, neutral 78-atom GROMACS ligand export; verified files |
| Assumed | Public 1UBQ inventory | 3.0 | 2 | Measured inventory and source link |
| Assumed | CHARMM36m ligand advice | 6.0 | 1 | CGenFF recommendation with Sage 1–4 compatibility caveat; advice only |
| Assumed | Aspirin / OpenFF | 21.2 | 4 | Neutral 21-atom ligand export, verified files |
| Assumed | Methylammonium / OpenFF | 9.0 | 3 | Eight-atom export preserves +1 formal/partial charge, verified files |
| Assumed negative | Undefined stereochemistry | 9.0 | 2 | Computed R/S choices; asks instead of choosing an isomer or parameterizing |
| Assumed negative | Deliberately absent CIF | 3.0 | 1 | Explicit missing-file diagnosis, no substitution or invented chains |

All nine completed their requested bounded behavior on the first attempt.
The missing-file case deliberately returned `FileNotFoundError`; this expected
rejection is retained rather than describing the interaction as error-free.
There were no unexpected tool failures. The undefined-stereo reply has a minor
redundant systematic-name typo ("2-butan-2-ol"); its preserved input, computed
isomeric SMILES and CIP labels are correct. No enantiomer was selected.

## CPU preparation versus waiting

| Ligand | AM1-BCC CPU-phase wall (s) | Helper preparation (s) | Worker incl. imports (s) | Chat outside worker (s) |
| --- | ---: | ---: | ---: | ---: |
| Actual 20-hydroxyecdysone | 89.072 | 89.706 | 93.637 | 5.804 |
| Assumed aspirin | 8.104 | 8.711 | 16.162 | 5.002 |
| Assumed methylammonium | 0.161 | 0.627 | 4.586 | 4.454 |

CPU-phase values are elapsed wall time of CPU work, not cumulative CPU-core
seconds. Chat minus worker includes provider, tools, delivery and observation
overhead; it is not a measurement of pure model reasoning. These single samples
are not a capacity distribution. No GPU is needed or claimed for OpenFF/SQM.

## What was checked beyond a completed chat

The existing `verify_agent_delivery.py` downloaded every delivered link through
the authenticated workbench path. All 30 ligand output files matched their
recorded byte counts and SHA-256 hashes; both structure links also returned
their actual files. The installed independent OpenFF tests recomputed molecular
identity from the exact input and exported SDF, checked topology atom counts and
partial charges, coordinate atom counts, finite recorded CPU energy and the
explicit absence of GROMACS preprocessing validation. Original request SMILES
also matched the actual tool argument and `preparation.json` byte-for-byte.

Manual scientific-scope review used the actual inventory/result files and
pinned ligand guidance, not just the generic verifier: author/label chains were
not merged, coordinate coverage was not called MD readiness, advice preserved
protein/ligand family compatibility, and an unspecified stereoisomer was not
guessed. A ligand in an artificial empty export box was not described as
solvated, equilibrated or ready for receptor/membrane MD. Finite energy and
export success do not establish force-field accuracy or engine equivalence.

The verification helper initially missed a plain-text exception without JSON
status. A narrow source-only regression now records that error and only permits
it for the explicitly named missing-file negative case. Transport/unrelated
failures are not waived. No product/runtime change or image rebuild was needed.

## Retained evidence and remaining work

Private root: `/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003`.

- `lynx-workloads-r1`: separate actual/assumed manifests and original read-only bindings.
- `lynx-agent-actual-r4`, `lynx-agent-assumed-r4`: actual prompts, chats, tool calls, operation/execution identities and per-artifact validation.
- `lynx-agent-actual-r4-verification.json`, `lynx-agent-assumed-r4-verification.json`: authenticated delivery/hash/chemistry checks.
- `lynx-agent-r4-error-scan.json`: explicit expected missing-file rejection, no unexpected failures.
- `lynx-agent-r4-cpu-evidence.json`: per-case timings and original input/measurement hashes.
- `client/mpinat-candidate-r4-20261003`: exact image, separate state, readonly bindings and native output files.

At this r4 CPU checkpoint, the hosted alanine starter example and 19 remaining MPINAT cases still needed
coordinated GPU admission. The five already native-complete benchmarks are
excluded from new work and have passed analysis-only recovery. The actual
customer production pipeline remains unqualified until its native input bundle
and protocol are supplied; the public alanine and MPINAT tests are explicitly
assumed controls. No customer default/endpoint promotion is claimed here.

## Prepared next execution, no admission yet

The isolated QA workspace initially lacked starter data (it is not a seeded
customer bucket). `stage_agent_inputs.py` copied the retained qualified public
v3 pack, verifying every file against its original manifest, and staged only
the 19 remaining MPINAT bundles. Original TPRs and native parameters remain
unchanged; only new MPINAT idempotency/output prefixes identify `candidate-6d8b2038`.
Inputs are mode 0444, not advertised as immutable mounts. Private CIF bindings
were neither copied nor changed. Five stager tests cover byte preservation,
completed-case exclusion and preventing overwrites/duplicate selection.

The installed `scientific_starter.resolve` passed without executing its returned
command. Actual input archive inspection confirms minimization followed by
20 ps NVT, 20 ps NPT and 20 ps production, each dynamics phase 10,000 × 0.002 ps.
This is not the original four-engine 1 ns study or a converged sampling claim.
`agent-staging-r4/{staging.json,hosted-alanine-preflight.json}` retain identities.
Dispatch only the separate `hosted-alanine-r4-manifest.json` and
`remaining-mpinat-r4-manifest.json` after the parent grants a GPU lane. Do not
start another admission-policy owner or reuse the old 73a3340a run identifiers.

## Follow-up: hosted example and r5 continuation

The separately assumed hosted alanine study subsequently executed on r4:
operation `bc1bb480-4536-4147-aae1-bbb59504d841`, conversation
`6461656a-2978-570c-bf83-c31679bff792`. It completed 13 native commands and
55 hash-verified files in a 150.4-second, eight-tool interaction. The original
6598-particle protocol remained 20 ps NVT + 20 ps NPT + 20 ps production,
with 21 production frames. It is a representative integration exercise, not
the customer's missing production pipeline or converged sampling.

Artifact inspection caught an invalid NVT `Density` selection despite the
analysis command returning zero. The original diagnostic is retained. A
separate versioned correction removes only that unavailable NVT observable;
an exact-image CPU replay verifies all 21 frames and unchanged values of the
four valid observables. NPT/production and physical inputs are unchanged.
See [STARTER_NVT_V4.md](STARTER_NVT_V4.md). This fixes the prepared next pack,
not the already published v3 data; no new GPU run or seed promotion is claimed.

The first remaining r4 benchmark pair exposed two further client defects:
MEM completed but its report lacked native timings, and PEP's completed upload
lost gzip metadata without ever submitting native work. R5
`b948ecca…ab927`, runtime source `61b4c3b`, adds durable native timing reports,
explicit encoding and measured transfer phases. Fresh analysis-only study
`96b89a35-9e48-5986-92ce-5bcdab33dad6` verified MEM's original three timing
rows and nine authenticated downloads without rerunning simulation. This is
successful recovery, not a rewritten first-attempt pass.

At 15:00 UTC, all nine CPU cases are being replayed once on that exact r5
candidate, while the 18 unexecuted public MD cases run separately. Current
progress lives in [STATUS.md](STATUS.md). Neither the older CPU passes nor
asynchronous admissions alone qualify the entire current customer release.
