# Cancer-immunotherapy fast-start qualification

Offline, task-owned qualification contract for the shared H100 PoC; it does not deploy or mutate the cluster. The matrix has 13 independent lanes: Proteina-Complexa, BoltzGen, mosaic, RFdiffusion, ESMFold2/Fast, Protenix v2, gated native academic AlphaFold3, OpenFold3 (primary available alternative), native academic BindCraft with PyRosetta, PyRosetta-free BindCraft, and separate RFdiffusion+ProteinMPNN+OpenFold3 and RFdiffusion+ProteinMPNN+BoltzGen workflows. Composite stages never share snapshot/cache evidence.

Regional OCI image cache, same-region shared artifact cache, host-RAM standby, and hot replicas are separate mechanisms. H100 nodes have no local NVMe, so node-local disk is unavailable. A GPU snapshot means **CUDA checkpoint + CRIU** with exact runtime/GPU/driver identity and post-restore semantic validation; it is not an image/artifact cache, warm RAM, or pod readiness signal. Every lane is currently `ready_for_live_trials: false` and snapshot support is gated or negative until immutable runtime assets merge.

The phase contract measures queue, provisioning, image/artifact transfer, deserialization, CPU-to-GPU load, graph/kernel and workflow initialization, semantic readiness, first semantic result, compute, drain and teardown. Promotion requires three clean prepared-node trials plus fresh-node and warm trials where capacity permits, using the existing generic fast-start policy.

Fixtures reference exact validator IDs without credentials, licensed weights, or secrets. Academic BindCraft requires approved PyRosetta wheel/license acceptance; native AlphaFold3 requires academic terms acceptance and restricted weight download. Missing artifacts remain blockers. Read-only inventory found no host PyRosetta/JAX/Torch/HuggingFace cache; the Protenix v2 weight probe returned HTTP 403.

```bash
python3 models/cancer-immunotherapy-fast-start/validate_contract.py
python3 -m unittest discover -s models/cancer-immunotherapy-fast-start/tests -v
```

Live H100 benchmarking starts only after sibling immutable runtime/source qualification merges. No deployment was performed for this task.
