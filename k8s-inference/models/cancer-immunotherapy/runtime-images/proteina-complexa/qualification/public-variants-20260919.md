# Proteina public variant study — 19 September 2026

Scope: ordinary scoped scientist04 public scientific-batch requests, complete
returned artifacts and independent raw/self-refolded geometry. This is not a
LibreChat acceptance, biological-efficacy result, or blanket customer-ready
verdict. The original failures remain retained.

The five-case study entered through release173, gateway image
`sha256:8ef559f18bec233b37fc16b5b8a16270c4685f44c72f07d0a9b336023ac58b52`.
Proteina runtime remained
`sha256:e5e075237a680dc01ace45b97ecfcfb9f95f5897aa51f36164591a74778a9cd1`
through the separately admitted release174 sensitivity case. Pinned scientific
source remains `NVIDIA-BioNeMo/Proteina-Complexa` revision
`54058860d43444c7289873f77d3e50b5b02348cd`. Runtime repair/publication and original
isolated failures are in the adjacent immutable variant receipts.

| Public operation | Variant / seed / steps | Designs | Raw basic geometry | Refolded basic geometry | End-to-end seconds |
| --- | --- | ---: | --- | --- | ---: |
| `1821f6e6-85e7-48e6-80b7-c7f1e299a09e` | FAD ligand /7 /400 |2|2 pass|2 pass|367.50|
| `8a56cfc3-f30b-464d-9a0e-3c5370f793e6` | FAD ligand /42 /400 |2|2 pass|2 pass|356.95|
| `af5eb0be-e9d3-49d2-9ebd-28f7f5274287` | LDH AME /7 /400 |1|1 pass|1 pass|262.19|
| `b28f435b-93f7-4583-a13c-9e651bbf4302` | LDH AME /42 /400 |1|1 pass|1 pass|257.34|
| `572a76f3-8130-4865-9cc5-354f1870f85a` | Original PDL1 /1 /100 |2|**2 fail**|2 pass|276.01|
| `3ddb929d-52c8-461e-b7e2-da3eedd0c1b0` | Separate PDL1 sensitivity /1 /400 |2|2 pass|2 pass|282.97|

The original five-case totals are **6/8 raw** and8/8 self-refolded basic geometry
passes. Do not combine the sensitivity case into a replacement “all passed”
cohort. For that paired case only diffusion_steps changed100→400; seed, target,
sample count and other original inputs remained fixed. Its independently
recomputed raw-to-refolded binder C-alpha RMSDs are0.42853/0.34162Angstrom and
agree with returned CSV values within0.01Angstrom. Required FAD/NAD/OXM cofactors
are present in the corresponding ligand/AME outputs. Every role is joined by
immutable artifact bytes, design ID and exact sequence, not filename order.

The public contract already requires an explicit diffusion-step count. There
was no hidden100-step default defect and no silent increase in user settings.
400 is a source-guided comparison baseline; shorter sampling remains an explicit
quality/runtime tradeoff. A source `num_samples=1` can produce multiple
sequence/refold designs; returned design IDs are counted explicitly.

## Evidence and limits

- Frozen five-case manifest SHA256:
  `540af875b28e599e576e592d9a5e50fa22468a7d444a52db4e713f2b45b614e8`.
- Separate sensitivity manifest SHA256:
  `8bfb339c711f5cc50c850e82f2da58346b6aa058ff7614f6c684d07fc4cd0536`.
- Protected consolidated public measurements SHA256:
  `5af7b663207263cb56daf1df4eb3ac18d780cb1e767a88e7732f6296797270ce`.
  This binds each public request/result/measurement hash and operation.
- Sensitivity measurement SHA256:
  `f2d4bfc62ba0b78c115dfd72bf778bd925b2d9e623392c3b6f91799c2d470278`.
- All six operations terminal; no extra test-owned GPU resources or active
  scientist04 operations remain. Earlier isolated candidate resources were
  cleaned separately. No quota, resources or execution limit changed.
- One terminal admin read returned503; bounded GET retry returned200. This is
  operational readback evidence, not a GPU failure or repeated inference.

Basic geometry is finite coordinates and bounded C-alpha geometry only—not
all-atom validity, target affinity, motif catalysis, experimental reproducibility
or clinical safety. End-to-end durations include queues and stage transitions;
they are not measured device utilization or billable GPU time. The repaired
execution is demonstrated at these public API identities. Final natural-language
customer workflow and unchanged-release cohort gates remain separate.
