# BoltzGen scientific-batch adapter

Candidate integration for upstream release `v0.3.2`, exact commit
`31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0`. The exact weight revision is
`c1be29e1f82ffcc72264f64b993c43fb4e0d17f0`; its six-file inventory includes
`boltzgen1_structuretrained_small.ckpt`.

The public envelope is the shared `scientific-run-request/v1` schema. This
directory owns identity evidence and examples only. `adapter.py` is a small
compatibility import; the implementation lives with the canonical controller
contracts in `components/control-plane/src/fs2_serve/scientific_batch/adapters`.
It accepts bounded batches and logical artifact IDs, while the controller alone
maps those IDs to absolute input, model, and work paths. Public commands, images,
environments, URLs, secrets, paths, unknown fields, and duplicate JSON fields
are rejected.

Each requested batch expands the checked-in catalog templates into upstream's
real protocol sequence. `boltzgen configure` validates the localized inputs and
resolves the pipeline, followed by one `boltzgen execute --steps ...` job per
selected step. Although configure does not execute model inference, the pinned
v0.3.2 image unconditionally queries CUDA device capability while constructing
the pipeline, so configure, design, inverse folding, folding, optional design
folding, and optional affinity each require one GPU per independent shard.
Analysis and filtering are CPU stages and do not mount model or molecule
artifacts, so their queue, lifecycle, and allocated-idle telemetry do not
overstate GPU use. The adapter only supplies a bounded expansion to the
canonical controller; it is not another scheduler.

The input tar layout is `design-specs/<shard>.yaml` plus any structure files
referenced by nested YAML `path` keys. The controller rejects absolute paths,
traversal, links, special files, duplicate archive members, duplicate YAML keys,
and size/count overruns before rewriting every path to its localized mount. A
reuse request may additionally contain `reusable-workspaces/<shard>/`; that
contained tree is materialized into the campaign workspace before configure.
The molecule artifact is mounted at the exact extracted `mols.zip` root
`/opt/fs2/artifacts/boltzgen-inference-molecules`, containing CCD pickle files,
not at a fabricated `mols/` child. Checkpoints are mounted read-only at the
canonical `boltzgen-checkpoints` cache root.

The profile declares restartable attempts with restart—not resumable
checkpoints. `reuse_completed=true` adds upstream `--reuse` only when the
controller localizes an explicitly supplied immutable reusable-workspace seed.
A preempted attempt otherwise starts again from committed inputs. The request
is capped at 32 shards, 10,000 designs per shard, 1,000 retained designs per
shard, and 60,000 designs total.

The collector reads upstream `final_designs_metrics_<budget>.csv` and the real
`final_<budget>_designs/*.cif` outputs into a canonical artifact manifest. The
offline validator checks immutable bytes, per-shard budget accounting,
canonical sequences, composition bias, interface confidence, refold RMSD,
unresolved-residue columns when emitted, and non-degenerate two-chain mmCIF
structures. Every ranking `file_name` is bound to one path-free structure
artifact identity, so an unrelated structure cannot satisfy the count alone.
Passing it does not qualify the model.

Run the focused suite from `components/control-plane`:

```bash
PYTHONPATH=src:../../catalog/runtime uv run pytest -q \
  tests/test_scientific_primary_adapters.py
```

The runtime contract is bound to the locally smoke-tested immutable image
digest, but the molecule dataset remains terms/readiness-blocked. The candidate
has no complete artifact manifest, H100 semantic receipt, exposed route, or
deployment readiness.
