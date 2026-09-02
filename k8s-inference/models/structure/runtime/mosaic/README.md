# mosaic scientific-batch adapter

This model-local package owns `mosaic-boltz2-proteinmpnn-v1`. It pins mosaic
commit `70fec525423f5f87156a1a957b4a4048f9f8e676`, the exact `recipe.json`, the
existing catalog Boltz2 manifest, and ProteinMPNN `v_48_020.pt`. Its image is
still `unbuilt-unqualified`: offline validation here is not an H100 or deployment
claim.

The public envelope and content-addressed input/output manifests reuse the
shared catalog schemas in `catalog/runtime/schema`; only mosaic parameters are
defined locally. A request supplies one immutable FASTA target and typed
hotspots, binder length, optimizer steps, shard count, and base seed. The
renderer creates direct-argv, suspended Kueue Job templates for deterministic,
independent GPU design shards and a CPU aggregate stage. The reconciler must
create only DAG-eligible nodes and bind the authoritative attempt identity when
applying a template. Aggregation writes a staging manifest and atomically
renames it only after all shards succeed. Cancellation terminates the current
attempt, while exit codes 75, 137, and 143 are retryable for at most three
attempts after controller-side infrastructure/preemption classification; the
Jobs themselves use `backoffLimit: 0` so Kubernetes cannot create untracked
retries.

The validator resolves artifacts by opaque ID, verifies every size and SHA-256,
binds shards and aggregate to the source, recipe, request, and admitted image,
and requires candidate metrics plus a complete binder-only PDB matching the
reported sequence. After that receipt passes, the shared controller owns the
terminal `scientific-run-result/v1` wrapper around the committed manifest
pointer. The two positive fixtures use distinct targets and shard
counts; the negative fixture proves arbitrary Python/objective input is closed
out by both schema and runtime validation.

Run from `k8s-inference`:

```text
PYTHONPATH=catalog/runtime python3 -m unittest discover -s models/structure/runtime/mosaic/tests -v
```
