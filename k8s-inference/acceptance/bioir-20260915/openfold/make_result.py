#!/usr/bin/env python3
"""Machine-readable lane summary derived from retained evidence, never forecasts."""
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--complete', action='store_true')
args = parser.parse_args()
measurements = json.loads((ROOT / 'measurements.json').read_text())
quality = json.loads((ROOT / 'quality.json').read_text())
resources = measurements['resources']
models = []
for model in ('openfold2', 'openfold3'):
    group = [r for r in resources if r['model'] == model]
    comparisons = []
    for candidate in [r for r in group if '-baseline-' not in r['resource'] and r['attempts']]:
        baseline = next((r for r in group if r['resource'] == f'{model}-baseline-{candidate["gpu"]}'), None)
        exploratory_rng = candidate['resource'] == 'openfold3-bir-float32-r5-h100'
        comparison = {'resource': candidate['resource'], 'gpu': candidate['gpu'], 'attempts': candidate['attempts'], 'valid': candidate['valid'], 'precision_changing': '-mixed-' in candidate['resource'], 'environment_control': '-upstream-env-' in candidate['resource'], 'exploratory_independent_diffusion_rng': exploratory_rng, 'ratios_not_scientific_equivalence': True, 'warm': candidate['warm'], 'baseline': baseline['resource'] if baseline else None, 'current_over_candidate_latency_ratio': {case: baseline['warm'][case]['median_seconds'] / values['median_seconds'] for case, values in candidate['warm'].items() if values and baseline and baseline['warm'].get(case)} if not exploratory_rng else None, 'mixed_http_queue': candidate['mixed'], 'gpu_allocation': candidate['gpu_allocation']}
        comparisons.append(comparison)
        resident_control = next((r for r in group if r['resource'] == f'{model}-upstream-resident-{candidate["gpu"]}'), None)
        comparison['lifecycle_control'] = '-upstream-resident-' in candidate['resource']
        comparison['graph_and_resident_combined_variant'] = '-graph-resident-' in candidate['resource']
        if comparison['graph_and_resident_combined_variant']:
            comparison['same_gpu_resident_control'] = resident_control['resource'] if resident_control else None
            comparison['resident_control_over_candidate_latency_ratio'] = {case: resident_control['warm'][case]['median_seconds'] / values['median_seconds'] for case, values in candidate['warm'].items() if values and resident_control and resident_control['warm'].get(case)}
            comparison['graph_state_evidence'] = f'raw/{candidate["resource"]}/runtime-after.json'
            runtime = json.loads((ROOT / comparison['graph_state_evidence']).read_text())
            comparison['graph_state'] = runtime.get('graph_state')
            comparison['skipped_cpu_transfers'] = runtime.get('skipped_cpu_transfers')
    models.append({'model_id': model, 'bir_fit': 'Public 0.1.0 exact checkpoint module supported; model-seam integration retains native preprocessing, confidence, artifact and HTTP schema.', 'status': 'partial', 'baseline': [r for r in group if '-baseline-' in r['resource']], 'comparisons': comparisons, 'quality': {'evidence': 'quality.json', 'metric': quality['metric'], 'samples': [q for q in quality['samples'] if q['model'] == model], 'limits': quality['limits']}, 'features': {'http_boundary_preserved': True, 'msa': 'Current wrapper rejects external MSA; unchanged dummy single-sequence features.' if model == 'openfold2' else 'Full inline native A3M and protein complex tested.', 'templates': 'Current wrapper rejects; not a BIR regression.', 'ligand_rna_dna': 'Not supported by current wrapper; no expanded-feature adoption claim.', 'batch': 'Single input only; concurrent HTTP queue measured, not tensor batching.', 'cancellation_idempotency': 'Not advertised by native wrapper and not qualified.', 'negative_probe_evidence': 'raw/*/feature-probes.json'}, 'snapshot': {'fresh_bir_snapshot_status': 'See independent sibling snapshot lane; not measured in this lane.', 'cuda_graph_enabled': False, 'graph_note': 'Public OF2 optimization registry has no graph modules.' if model == 'openfold2' else 'Default BIR kernels do not enable graphs; native Lightning CPU/GPU teardown needs lifecycle qualification.'}, 'recommendation': 'Conditional mixed-precision prototype only; do not adopt FP32 variant on measured L40S shapes. No promotion.' if model == 'openfold2' else 'Pending complete native-RNG comparator and scientific quality; no promotion.', 'limitations': ['Small public corpus, three warm repeats per shape, no tail-SLO or efficacy claim.', measurements['timing_boundary'], measurements['allocation_warning'], 'H100 and L40S are separate same-physical-GPU pairs.', 'Current baseline already persistent; no residency speedup attributed to BIR.', 'No production API/gateway/MCP heavy traffic or routing changes.', 'Failed starts are retained; pure GPU-forward and busy-GPU time unmeasured.'] + (['OF3 joint dependency overlay conflicts with declared BIR SciPy/Gemmi pins and needs qualification.', 'r5 explicit private seed42 noise stream is exploratory; corrected candidate preserves native global seed42 stream.'] if model == 'openfold3' else []), 'evidence': ['report.md', 'measurements.json', 'quality.json', 'fixtures/cases.json', 'manifests/', 'raw/'], 'failed_start_resources': [r['resource'] for r in group if r['startup_failed_without_requests']]})
for model in models:
    model['status'] = 'measured' if args.complete else 'partial'
    if model['model_id'] == 'openfold2':
        model['recommendation'] = 'Reject measured FP32 implementation for performance on both GPUs; mixed precision is a conditional prototype requiring broader accuracy qualification. No promotion.'
        model['snapshot'] = {'fresh_bir_snapshot_status': 'See ../snapshot/result.json: separate OF2 fresh-pod qualification.', 'cuda_graph_enabled': False, 'graph_note': 'Public OF2 optimization registry has no graph modules.'}
    else:
        model['recommendation'] = 'HOLD FP32 graph/resident serving: measured distinct-query-ID gains and close H100 quality are potential only; matched normal workers fail when reusing the warmed request key (see snapshot/openfold3). Repeated-key graph/lifecycle robustness is unqualified, and failure is not isolated to checkpoint restoration. Hold mixed precision for structural drift. No promotion.'
        model['snapshot'] = {'fresh_bir_snapshot_status': 'Complete negative application qualification in ../snapshot/openfold3/result.json: 3 low-level restores ready, 0 restored inference passes; all 3 ordinary-load controls also fail on the warmed query key. No restoration-only causal claim, output parity or valid-result startup benefit. Distinct-key normal-load fallback passes; cross-node portability untested.', 'cuda_graph_enabled': True, 'graph_note': 'Bounded GPU-resident wrapper enables actual diffusion GRAPH_VERIFIED state; default r5/r6 runs had no graphs. Request metadata creates distinct graph keys, so recapture overhead remains in HTTP timings. Cached-key reuse fails in independent normal controls; HOLD graph-serving adoption.'}
        model['limitations'].append('Graph variant and upstream-resident lifecycle control are paired on H100; L40S current-baseline ratios include residency changes and do not isolate graph effects.')
        model['limitations'].append('Core graph benchmark used distinct query IDs, causing recapture. Subsequent identical-key normal-worker challenge failed with CUDA illegal memory access. No graph cache reset/invalidation fix was implemented or measured.')
contract = json.loads((ROOT / '../../../models/structure/batch-adapters/openfold3/contract.json').resolve().read_text())
models.append({'model_id': 'openfold3-openbind', 'bir_fit': 'Unverified checkpoint/version compatibility; public BIR openfold3 registry explicitly maps Preview2, not OpenBind.', 'status': 'unsupported', 'baseline': {'scope': 'Separate applicability review, no fresh OpenBind benchmark requested in this lane.', 'adapter_contract': contract}, 'comparisons': [], 'quality': {'measured': False}, 'features': {'identity': 'Separate OpenBind v0.5.0 batch adapter; never substituted for Preview2.'}, 'snapshot': {'fresh_bir_snapshot_status': 'unverified'}, 'recommendation': 'Do not replace with public Preview2 BIR weights. Separate converter/checkpoint and feature qualification required.', 'limitations': ['Family resemblance is not checkpoint compatibility.', 'No OpenBind speedup or scientific equivalence measured.'], 'evidence': ['report.md', '../../../models/structure/batch-adapters/openfold3/contract.json', '../boltz2/vendor/bioir/bionemo_ir/hubs/hf.py']})
(ROOT / 'result.json').write_text(json.dumps({'evaluation_date': '2026-09-15', 'state': 'complete' if args.complete else 'running', 'models': models}, indent=2) + '\n')
print(json.dumps({'models': [m['model_id'] for m in models]}))
