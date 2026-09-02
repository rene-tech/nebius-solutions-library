# RFdiffusion typed scientific-batch adapter

This directory adds the model-local backend `rfdiffusion-upstream-v1-1-0-base`
for model ID `rfdiffusion-upstream`. It deliberately pins stable tag `v1.1.0`
at `9273ef67335acaf91df0150473a274759229cdf6`, Base checkpoint SHA-256
`0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca`,
and the canonical artifact inventory. The later observed upstream HEAD is
recorded separately. Existing HTTP/B300 files are unchanged and are not evidence
for this unbuilt, unqualified H100 batch backend.

The request and artifact envelopes reuse shared catalog schemas. The local
parameter schema exposes only typed generated/motif contigs, typed hotspots,
diffusion steps, and deterministic seed sharding; raw Hydra overrides are
invalid. Motif/hotspot runs require one content-addressed target PDB, while
unconditional runs require the exact typed design-context artifact.

Direct-argv GPU shard Jobs feed one CPU aggregate Job, all initially suspended
for Kueue. The DAG defines bounded retry/cancel behavior and atomic manifest
publication. Semantic validation binds every shard to source/checkpoint/seed,
checks content digests and contig length, parses complete non-degenerate PDB
backbones, and recomputes motif backbone RMSD from the input and output
structures. Two positive fixtures cover unconditional and motif/hotspot modes;
the negative fixture proves raw Hydra input is rejected. The shared controller
wraps the committed output pointer and semantic receipt in
`scientific-run-result/v1`.

Run from `k8s-inference`:

```text
PYTHONPATH=catalog/runtime python3 -m unittest discover -s models/structure/runtime/rfdiffusion/tests -v
```
