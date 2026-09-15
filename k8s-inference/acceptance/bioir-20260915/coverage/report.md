# Coverage baseline report — 2026-09-15

Current serving images were cloned into isolated, explicitly scheduled workers. No BIR full-model path exists for these eight models; all BIR speedups are N/A. See [applicability](applicability.md) for source-level seams and related-product assessment.

| Model | Attempted / artifact-valid / product-successful | GPUs | Warm/request median ranges | Total allocated GPU-s per product-success | Status |
|---|---:|---:|---:|---:|---|
| diffdock | 42 / 21 / 21 | 1 | 1.7850–3.0699 s | 13.53 | measured; released |
| evo2-40b | 21 / 21 / 21 | 2 | 1.3431–5.9213 s | 53.81 | measured; released |
| genmol | 21 / 21 / 21 | 1 | 0.2276–0.3793 s | 3.20 | measured; released |
| molmim | 21 / 21 / 0 | 1 | 0.1909–2.9746 s | N/A | measured; released |
| msa-search-pdb70 | 30 / 30 / 30 | 0 | miss 2.187–2.202 s; cached 1.11–1.15 ms | N/A | measured; released |
| proteinmpnn | 28 / 28 / 28 | 1 | 0.2196–0.4248 s | 3.80 | measured; released |
| rfdiffusion | 12 / 12 / 12 | 1 | 40.7960–63.6825 s | 70.72 | measured; released |
| proteina-complexa | 13 / 9 / 9 | 1 | 155.0315–194.9999 s | 276.83 | measured; released |

## Interpretation

- MolMIM: 21 valid response envelopes are not 21 newly generated molecules. All measured molecules were input fallbacks (`model_decoded=false`); the 4-molecule case returned only one incumbent. The current server documents this fallback. Address generation success/cardinality before offering performance claims; this evaluation makes no production change.
- MSA: roughly 2.2-second real searches and millisecond cached-A3M returns are separate cohorts. This service allocates 0 GPUs, so GPU-hour efficiency does not apply.
- Evo2: same 40B checkpoint, 2 H100s and native precision; first-shape cost differs from repeated requests. Measured replay rejection 409 and unknown-field rejection 400. Existing customer pods were not modified.
- DiffDock: first harness used the wrong route; all 21 HTTP 404 attempts are retained. The corrected native route produced 21 valid poses. Reported warm timings use corrected calls only; total resource cost includes the failed attempt period.
- RFdiffusion: actual 50-step generation, 80/160/256-residue backbones and native batch 2, with 3 repetitions. It is a new-process runtime; labeling it warm persistent would be wrong. The assigned L40S was used, so these are L40S results, not H100 extrapolations.
- Complexa: status and actual stage evidence are recorded in the structured result. A generation-only result cannot stand in for the complete generate/filter/evaluate/analyze workflow.

## Evidence and reproduction

[result.json](result.json) contains every case median, individual repetition, dispersion, exact image digest, runtime metadata and cleanup state. [raw](raw) retains request/response JSONL, failures and server logs. [inventory](inventory) freezes live deployments and scientific execution mappings. [lifecycle](lifecycle) records image/start/stop events. Public input structures are 1UBQ/1LYZ/1MBN and are pinned in [fixtures](fixtures).

`prepare.py` prepares task-owned fixtures/ConfigMaps; `control.py clone` copies the frozen current HTTP runtime; `run.py` validates ConfigMap propagation then executes the in-pod client; `batch_control.py` and `batch_client.py` run scientific batch images; `cleanup.py` only deletes a named pod after verifying evaluation/lane labels; `analyze.py` regenerates this report. Explicit node and namespace choices follow the shared contract and manager exceptions.

No default-network API benchmarks or production routing modifications occurred. Unsupported cancellation, broad feature parity, snapshot restore and paired BIR scientific quality remain unverified, not inferred from successful schema checks.

## Complexa scientific scope and preserved failure

Nine timed complete workflows cover PDL1 native batches 1/2 and TNFalpha batch 1, with three seeds per case and 25 generation steps from the current positive fixture. Optional ESM/monomer/designability evaluation is disabled as in the current controller; AF2 self evaluation is retained. The evaluation CSV reloads default generation metadata and says 400 steps; retained generation argv/logs are authoritative for the actual 25 steps. This metadata discrepancy is preserved. Ligand and AME variants were not measured.

The final long-running exec stream disconnected while emitting its completed result. Its truncated row, stderr and artifacts remain; no timing was imputed. Only that case was repeated with a durable per-trial JSON record. Allocation cost includes both attempts. Independent validation checks every retained PDB coordinate and native AF2 confidence/scRMSD metrics; see `raw/proteina-complexa-independent-validation.json` for scientific pass counts. Artifact-valid requests do not imply successful binder designs.

## Version and isolation evidence

`inventory/*-build-inputs.json`, immutable image digests in every manifest, and frozen runtime identities pin source/checkpoint versions. `inventory/proteinmpnn-checkpoint-pin.json` records the current catalog checkpoint manifest; the exact benchmark image supplies that runtime. Complexa source/model pins are in `inventory/proteina-complexa-image-lock.json`; both 7.0 GB of protein checkpoints were SHA256-verified from the read-only evaluation mount after benchmark completion. `inventory/evo2-shared-host-after-health.json` records the original six customer GPU pods remaining ready with unchanged resources/images. No production state was changed.
