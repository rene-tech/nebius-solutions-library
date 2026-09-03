# Native academic BindCraft/PyRosetta scientific-batch adapter

This model-local package owns model ID `bindcraft` and backend
`bindcraft-v1-5-3-pyrosetta-academic`, pinned to the source-qualified commit
`7cd4ace1b7407adf66a50dfefa47de2270f5e4a9` immediately preceding the
`v.1.5.3` release. The diverged tag itself is deliberately not used. This
backend cannot silently fall back to the open alternative. The code is MIT,
while PyRosetta remains an academic, tenant-private prerequisite. No
credential, PyRosetta package, installed tree, or licensed package data is
stored in this repository.

Requests reuse the shared scientific request and artifact-manifest schemas and
carry only target-structure references plus typed model parameters. Academic
authorization and installation readiness are deployment-owned state, never
caller-supplied request fields. The owner-authorized academic POC does not use
a per-request license receipt. Every ordinary Job mounts the already-installed
`pyrosetta-bindcraft/site-packages` tree from
`academic-assets-runtime-rwx` read-only at
`/opt/fs2/academic/pyrosetta-bindcraft/site-packages`, puts that exact path
first in `PYTHONPATH`, and joins supplemental group `65532`. It deliberately
omits `fsGroup`, avoiding a recursive ownership walk over the 9,477-path tree.
The `ArtifactMaterialization` identity is
`bindcraft-pyrosetta-installed-tree`, with `fs2-tree-manifest/v1` digest
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`
and 3,287,122,494 bytes. The source wheel remains separate provenance with
SHA-256 `4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242`
and 1,667,097,173 bytes.

Independent deterministic GPU trajectory shards feed one CPU aggregate stage.
Commands are direct argv, Kueue Jobs start suspended, and the aggregate is
published by atomic rename only after every shard succeeds. The output validator
checks exact backend/source/image continuity, complete shard accounting,
the `pyrosetta` scoring engine, bounded interface metrics, and a binder-only PDB
whose sequence matches the candidate record. Static adapter metadata binds the
exact wheel digest, installed-environment digest, installer evidence, and use
authorization without introducing request-time admission. The shared
controller, not this model package, owns the terminal
`scientific-run-result/v1` wrapper.

The published image at digest
`sha256:9ec7eb93208ffd5ec88669e9a6714d8d1e9bffcea1bd5130ab81271095736aa1`
passed a full bounded H100 trajectory on 2026-09-03: 140 design iterations,
ProteinMPNN sequence generation, AF2 validation, PyRosetta relaxation/scoring,
filtering, reranking, atomic aggregation, and this adapter's independent output
validator. The route remains deliberately unexposed because this task publishes
images and evidence but does not deploy a model service.

Run from `k8s-inference`:

```text
PYTHONPATH=catalog/runtime python3 -m unittest discover -s models/structure/runtime/bindcraft-native/tests -v
```
