# BoltzGen H100 qualification

This directory records the original closed H100 qualification of the upstream
BoltzGen v0.3.2 `design` stage and the model-owned public acceptance input used
by the active fleet bridge. The checked-in renderer imports the production
scientific adapter, compiles its request into direct `boltzgen configure` and
`boltzgen execute` argv, and emits a suspended Kueue Job with immutable image
and artifact identities. The image-level qualifier itself did not open a
route; the aggregate activation is a later, separately validated step.

## Locked subjects

| Subject | Immutable identity |
| --- | --- |
| Upstream source | `HannesStark/boltzgen@31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0` (`v0.3.2`, MIT) |
| H100 runtime | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/boltzgen@sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e` |
| Checkpoints | `boltzgen-checkpoints/sha256/ab822047e2d6c5fc4c3fabb35d15b611c96851ec4946304c661ecfc634acbbdf` |
| Molecule dictionary | `boltzgen-inference-molecules/sha256/8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc` |
| Target source | RCSB `5J89.cif`, SHA-256 `321a604f520452820ef9333fd2710b25d1529f92c5c736d500aac454bdbbbfe3` |
| Offline target projection | chain A polymer, both mmCIF chain namespaces normalized to A, SHA-256 `93aeba8e72dcb98589f5da5ac5379f0c81f676cbf704a77a7d977faeb6c7ed19` |

The image lock also records the source archive, Dockerfile, dependency lock,
SBOM, OCI config, scanner result, every checkpoint digest, artifact markers,
adapter source, cluster identity, and the explicit B300 prohibition.  The
checkpoint localization receipt records which predecessor bytes were reused
and the content-addressed publication result.

## Reproduce the contract checks

From this directory:

```bash
./run_checks.sh
uv run --with gemmi==0.7.5 python qualification/fetch_target.py \
  --output /tmp/fs2-boltzgen-pdl1-chain-a.cif
uv run --project ../../../../components/control-plane \
  python qualification/render_job.py \
  --scenario cold \
  --pool-id h100-1x \
  --target /tmp/fs2-boltzgen-pdl1-chain-a.cif \
  --output /tmp/fs2-boltzgen-cold.json \
  --plan-output /tmp/fs2-boltzgen-cold-plan.json
```

The fetch helper is the only online preparation step.  It verifies the full
RCSB object before emitting the deterministic projection.  The rendered Job
has deny-all ingress and egress, mounts both published generations read-only,
uses the `inference-models` LocalQueue and `batch` priority, requests exactly
one H100, and has no service-account token.  Set `--node-name` when rendering a
prepared comparison so it runs on the same node as the cold attempt.

The public submission uses two distinct artifact uploads.  First upload the
deterministic gzip-compressed campaign tar as `application/gzip` with
`compression: gzip`; its members are `design-specs/<shard>.yaml` and
`5J89-chain-A.cif` (the materializer supplies the surrounding `inputs/`
directory).  Put that artifact pointer in the single `campaign-input` entry of
`qualification/manifest-template.json`, serialize the manifest as canonical
JSON, and upload it as `application/vnd.fs2.scientific-manifest+json` with no
compression.  Finally put the manifest artifact pointer—not the campaign
pointer—in `qualification/request-template.json` before submitting.  The two
artifact IDs, digests, sizes, media types, and compression declarations remain
independent; `application/x-tar` is not in the deployed artifact media-type
allowlist.

The route-level public acceptance request deliberately generates twenty
bounded candidates (the adapter's per-batch maximum) and retains the best one.
A smaller smoke can legitimately produce no result when BoltzGen's own
composition filters reject every design; using the bounded maximum makes the
example robust without weakening the scientific output validator or changing
the one-result response contract.

## Semantic boundary

The independent validator imports neither the BoltzGen runtime nor the
platform adapter.  It parses the emitted mmCIF and NPZ with Gemmi and NumPy and
requires one physical 60–80-residue non-target designed chain, the unchanged PD-L1 target
sequence, complete backbone atoms, non-degenerate geometry, the requested
PD-1-contact face, a physical 2–8 Å closest contact, and at least three binder
residues within 12 Å of that face.  The runtime currently canonicalizes the
request's designed chain C to output chain B; the validator binds its semantic
identity through the NPZ design mask and interface rather than assuming that
the input chain label survives.  It records exact output digests and sizes.

Qualification covers `configure` plus the upstream `design` stage for one
representative PD-L1 request.  Inverse folding, folding, affinity, analysis,
filtering, controller collection, result publication, and route-level access
remain public acceptance work. The catalog profile now consumes the exact
qualified image and artifact handoff in `active` state so that the public
runner can break the activation/acceptance circularity. Public completion and
scheduler-eligibility receipts remain null until that run succeeds; the
profile must not be promoted to `qualified` before then.

## Live evidence

`evidence/h100-qualification-receipt.json` contains the cold and prepared phase
timelines, exact Kueue Workload, Job, Pod, node and GPU identities, immutable
outputs, cleanup proof, and the before/after shared-workload checks.  The live
campaign uses only the `h100-1x` preemptible pool in project `project-e00rene`,
region `eu-north1`; it does not use B300 or change any quota or pool limit.

The final cold run started from a new node and an absent image.  The 3.64 GB
image pull took 69.576 seconds; model-ready, first-result and design-complete
were 33.433, 50.496 and 53.599 seconds from design start.  The independently
accepted 75-residue binder has a 5.394 Å closest contact and 28 binder residues
within 12 Å of the requested PD-L1 face.  The prepared run observed the exact
image already present, reached model-ready in 34.241 seconds and independently
accepted a distinct 60-residue binder.  Exact private cloud resource IDs are
kept in the Agent Task Deck evidence rather than the public solution export;
the public receipt retains Kubernetes UIDs, node boot IDs, GPU UUIDs and all
content identities needed to join the two records.
