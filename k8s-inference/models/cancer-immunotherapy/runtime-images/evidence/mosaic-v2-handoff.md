# Mosaic adapter-v2 H100 handoff

Ownership moved to Agent Task Deck task
`fs2-cancer-images-mosaic-runtime-qualification-r20260903` and tmux session
`agent-fs2-cancer-images-mosaic-runtime-qualification-r20260903`.  The combined
image task must make no further Mosaic code changes or GPU launches.

## Immutable identities

- Upstream: `https://github.com/escalante-bio/mosaic` at
  `70fec525423f5f87156a1a957b4a4048f9f8e676`; source archive SHA-256
  `41d30b2ac41920c952087a642882c6e8ef1a31a4565db5667a372c9f467b2d1a`.
- Adapter contract: commit `6551d8708c829fe99d229d1f547b8bd8cab0231e`,
  `/opt/fs2/bin/mosaic-batch` SHA-256
  `edd017c67e548b6e5d74cd700b0665bd05b09cd16eb82afaaf14fa7f25d046e7`,
  recipe SHA-256
  `cbfc7a88e6e7c2255730218bbdeaf6fc272d721b6c792231429a923309a8e0fe`.
- Published non-overwritten image:
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/mosaic@sha256:4d17ec790bc5962b095b97b298c9f65832a51d9ea4590b51d532e46db48df1ca`
  (tag `70fec525423f5f87156a1a957b4a4048f9f8e676-adapter-v2`).
- `adapter-v3` is absent from the registry (confirmed `MANIFEST_UNKNOWN` at
  2026-09-02 23:50 UTC).  A local-only interrupted successor build must not be
  treated as published or qualified.
- Default image contains no weights.  SBOM SHA-256 is
  `c7373192d9a0bec87e2038a723dd0f10fba439ceec4a839365d980ab847a90be`.

## Exact external artifacts

The preserved PVC is `fs2-models/fs2-runtime-qualification-artifacts-r20260902`.
Its staging receipt records:

- Boltz2 checkpoint, revision `6fdef46d763fee7fbb83ca5501ccceff43b85607`:
  `mosaic/boltz/boltz2_conf.ckpt`, 2,286,561,469 bytes,
  SHA-256 `090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1`.
- Boltz2 molecules, same revision: `mosaic/boltz/mols.tar`, 1,855,662,080
  bytes, SHA-256
  `39e076d96dbec6b4e86982bbda16f3a53a2a60c9bdc17828d88f6f9a0c7d1fd7`;
  extracted `mols/*.pkl` is present.
- ProteinMPNN checkpoint, Mosaic source revision:
  `mosaic/proteinmpnn/v_48_020.pt`, 6,681,301 bytes, SHA-256
  `c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd`.
- Target FASTA: `inputs/artifact.mosaic.target.minibinder`, 44 bytes,
  SHA-256 `7ed5501be9de5478ba1e86ee23edb0f08bc650a10b83671e01f1b383ef51b1bc`.

Source discovery: Mosaic passes `ccd_path=cache/ccd.pkl` but calls Boltz
`process_inputs(..., boltz2=True)`.  The installed Boltz source at
`/tmp/fs2-mosaic-deps.troxVK/boltz/src/boltz/main.py:764` uses
`load_canonicals(mol_dir)` for Boltz2 and opens `ccd_path` only for Boltz1.
Therefore the adapter-v1 `ccd.pkl` guard was incorrect; the extracted
`mols/*.pkl` tree is the required external input.

## Real H100 execution

Context: project `project-e00rene`, region `eu-north1`, H100 cluster
`mk8scluster-e00j5z9te7x5dd9g6a`, namespace `fs2-models`, preemptible H100
SXM5 80 GB node `computeinstance-e00phxdecf401f6rq5`.

Job `fs2-model-forward-mosaic-v2-20260903` (UID
`8a5ae44a-1cc0-4fd4-a5d4-c86f0cadce42`) and Pod
`fs2-model-forward-mosaic-v2-20260903-jn75s` (UID
`ccdd4a70-c0a3-466c-92b7-e049ebbc6950`) are preserved.  The container ran
2026-09-02 23:40:38--23:44:27 UTC and exited 0.  Exact generated argv:

```text
/opt/fs2/bin/mosaic-batch run-shard --request /var/run/fs2/request.json --input-manifest /var/run/fs2/input-manifest.json --recipe /opt/fs2/mosaic/recipe.json --recipe-sha256 cbfc7a88e6e7c2255730218bbdeaf6fc272d721b6c792231429a923309a8e0fe --shard-index 0 --seed 7300 --output /workspace/runs/3ff8c2dd57132a60/shards/000
```

The actual H100 log reports optimizer loss falling from 15.48 to 12.38 over
20 steps and ends with:

```json
{"backend_id":"mosaic-boltz2-proteinmpnn-v1","candidate_sequence":"VGLALYCLWPELFDGDAEEHHDEEALSEGKLPNEAFLAIL","gpu":"NVIDIA H100 80GB HBM3","seed":7300,"shard_index":0,"status":"succeeded"}
```

Aggregate Job `fs2-aggregate-mosaic-v1-20260903` (UID
`3baab528-d9cc-45d1-a089-c03b88a7ee16`) exited 0 with the exact adapter argv:

```text
/opt/fs2/bin/mosaic-batch aggregate --request /var/run/fs2/request.json --input-manifest /var/run/fs2/input-manifest.json --shards /workspace/runs/3ff8c2dd57132a60/shards --expected-shards 1 --staging-manifest /workspace/runs/3ff8c2dd57132a60/output-manifest.json.tmp --output-manifest /workspace/runs/3ff8c2dd57132a60/output-manifest.json --atomic-rename
```

## Remaining semantic blocker

Job `fs2-validate-mosaic-v1-20260903` failed closed.  The output inspector Job
`fs2-inspect-mosaic-output-v1-20260903` proved the generated candidate PDB has
40 residues and 200 atoms with finite, nondegenerate coordinates, but every
residue is named `UNK`.  The candidate metrics carry the designed canonical
sequence, while `prediction.st.make_pdb_string()` loses those residue names.
The dedicated task should fix serialization by assigning the 40 designed
one-letter identities to the 40 PDB residue records, publish a new unique tag,
rerun the exact generated argv on H100, and require the adapter semantic
validator to pass.  Do not claim Mosaic Ready from the successful forward
alone.

Preserved live objects include the v2 forward, aggregate, failed validator,
output inspector, artifact inspector, staging/input Jobs, task PVC, and
qualification ConfigMaps.  The dedicated task owns their eventual cleanup.
