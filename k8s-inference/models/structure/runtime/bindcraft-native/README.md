# Native academic BindCraft/PyRosetta scientific-batch adapter

This model-local package owns model ID `bindcraft` and backend
`bindcraft-v1-5-3-pyrosetta-academic`, pinned to the source-qualified commit
`7cd4ace1b7407adf66a50dfefa47de2270f5e4a9` immediately preceding the
`v.1.5.3` release. The diverged tag itself is deliberately not used. This
backend cannot silently fall back to
the open alternative. The code is MIT, but this backend remains
academic-access-gated because it requires PyRosetta. No credential, PyRosetta
package, or licensed package data is stored in this repository.

Requests reuse the shared scientific request and artifact-manifest schemas and
carry only target-structure references plus typed model parameters. Academic
authorization is operator-owned admission state, never a caller-supplied
request field. Rendering fails closed without a strong access-receipt digest;
every Job template mounts only its non-secret receipt evidence from a ConfigMap
and runs `/opt/fs2/bin/verify-academic-access` before execution.

Independent deterministic GPU trajectory shards feed one CPU aggregate stage.
Commands are direct argv, Kueue Jobs start suspended, and the aggregate is
published by atomic rename only after every shard succeeds. The output validator
checks exact backend/source/receipt/image continuity, complete shard accounting,
the `pyrosetta` scoring engine, bounded interface metrics, and a binder-only PDB
whose sequence matches the candidate record. The shared controller, not this
model package, owns the terminal `scientific-run-result/v1` wrapper. The runtime image remains blocked
pending private academic-asset ingestion and has not been H100-qualified.

Run from `k8s-inference`:

```text
PYTHONPATH=catalog/runtime python3 -m unittest discover -s models/structure/runtime/bindcraft-native/tests -v
```
