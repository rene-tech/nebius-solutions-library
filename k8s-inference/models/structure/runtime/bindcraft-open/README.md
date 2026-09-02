# Open BindCraft alternative scientific-batch adapter

This model-local package owns the separate model ID
`freebindcraft` and backend `freebindcraft-v1-0-5`, pinned to FreeBindCraft tag `v1.0.5` at
`28c43fc48942eebd7918f504e9812c5c17bb3411`. It is not a transparent substitute
for native academic BindCraft. Its composite dependency terms remain
`component-review-required`, and its image remains `unbuilt-unqualified`.

The adapter reuses the shared scientific request and content-addressed artifact
manifests while keeping only FreeBindCraft parameters local. Deterministic GPU
trajectory shards feed a CPU aggregate stage; Jobs start suspended for Kueue,
use direct argv, and publish the final manifest atomically. Every trajectory
explicitly passes `--no-pyrosetta` and `--pyrosetta-forbidden`, and the artifact
lock forbids PyRosetta.

Validation requires the exact open backend, `standard` execution evidence,
`pyrosetta=forbidden`, the `openmm-freesasa` scoring engine, complete shards,
bounded OpenMM/FreeSASA/interface metrics, and a binder-only PDB matching the
candidate sequence. The shared controller owns the terminal
`scientific-run-result/v1` wrapper. Two positive fixtures cover distinct panels; the negative
fixture selects the native parameter schema and is rejected without fallback.

Run from `k8s-inference`:

```text
PYTHONPATH=catalog/runtime python3 -m unittest discover -s models/structure/runtime/bindcraft-open/tests -v
```
