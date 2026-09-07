# Full scientific regression — 7 September 2026

All ten scientific profiles passed a fresh request through the deployed public
API after the full-fleet Terraform rollout from source
`dbe67b216954d22ed32c3ae4b76f4653f425ad1f`. Four concurrent workers exercised the
existing queue and GPU pools without changing their limits. The aggregate
records ten succeeded, zero failed; per-profile receipts retain the existing
semantic, runtime and artifact checks.

This regression supplements each profile's earlier two-input qualification
and three startup trials. It is not ten new cold-start measurements, and a
successful normal fallback must not be called a GPU snapshot restore.

The separate production snapshot receipt in `../snapshots/` binds Protenix's
actual CUDA+CRIU restore to its successful public operation. GPU queue time and
full scientific execution are distinct from restore time. Completed Job logs
remain retrievable through the existing authenticated public Grafana/Loki API,
even after Kubernetes removes the successful Pod.

The model IDs are AlphaFold3, BindCraft, BoltzGen, ESMFold2, ESMFold2-Fast,
Mosaic, OpenFold3/OpenBind, Proteina-Complexa, Protenix v2 and RFdiffusion.
The fixed expected inventory remains `../expected-models.json`.
