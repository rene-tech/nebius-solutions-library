#!/usr/bin/env python3
"""Derive snapshot lifecycle, structural parity and graph claims from receipts."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent / 'openfold'))
from score_outputs import parse, coordinates, metrics

def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default
def utc(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
def stat(values):
    return {'n': len(values), 'values': values, 'median': statistics.median(values), 'min': min(values), 'max': max(values)} if values else None
def first_json(path):
    try:
        return json.JSONDecoder().raw_decode(path.read_text().lstrip())[0]
    except (OSError, ValueError):
        return None

cases = {v['id']: v for v in read(ROOT.parent.parent / 'openfold/fixtures/cases.json')}
trials, artifacts = [], {}
for manifest in sorted((ROOT / 'manifests').glob('fs2-bioir-of3-*.json')):
    value = read(manifest)
    if value.get('kind') != 'Pod' or value['metadata'].get('labels', {}).get('role') == 'bundle-inventory':
        continue
    name = value['metadata']['name']
    raw = ROOT / 'raw' / name
    ready = read(ROOT / 'lifecycle' / f'{name}-ready.json')
    released = read(ROOT / 'lifecycle' / f'{name}-released.json')
    pod = read(ROOT / 'lifecycle' / f'{name}-before-delete.json') or read(ROOT / 'lifecycle' / f'{name}-observed.json')
    created = read(ROOT / 'lifecycle' / f'{name}-created.json', {})
    cohort = 'restore' if '-restore-' in name else 'normal' if '-normal-' in name else 'fallback' if name.endswith('-fallback') else 'donor'
    attempts = []
    for path in sorted(raw.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            row.pop('request', None)
            row['schedule'] = path.stem
            attempts.append(row)
    runtime_states, structures = {}, {}
    archive = raw / 'requests.tgz'
    if archive.exists() and archive.stat().st_size:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if '/measured/' in member.name and member.name.endswith('.response.json'):
                    response = json.load(tar.extractfile(member))
                    result = response['outputs'][0]['structures_with_scores'][0]
                    case = Path(member.name).name.split('-', 1)[1].removesuffix('.response.json')
                    structures[case] = result
                elif member.name.endswith('.runtime.json'):
                    runtime = json.load(tar.extractfile(member))
                    runtime_states[member.name] = {'graph_state': runtime.get('graph_state'), 'skipped_cpu_transfers': runtime.get('skipped_cpu_transfers')}
    artifacts[name] = structures
    worker = (raw / 'supervisor.log').read_text() if (raw / 'supervisor.log').exists() else ''
    started = creation = scheduled = None
    if pod:
        creation = utc(pod['metadata']['creationTimestamp'])
        states = pod['status'].get('containerStatuses', [])
        if states:
            state = states[0].get('state', {})
            when = state.get('running', state.get('terminated', {})).get('startedAt')
            started = utc(when) if when else None
        scheduled = next((utc(c['lastTransitionTime']) for c in pod['status'].get('conditions', []) if c['type'] == 'PodScheduled' and c['status'] == 'True'), creation)
    source_release = read(ROOT / 'lifecycle' / (created.get('source_pod', '') + '-released.json'))
    capture = read(ROOT / 'lifecycle' / f'{name}-capture.json')
    first_valid = min((r['completed_unix'] for r in attempts if r.get('valid')), default=None)
    trials.append({'pod': name, 'cohort': cohort, 'ready': bool(ready), 'released': bool(released), 'container_to_observed_ready_seconds': ready['unix'] - started if ready and started else None, 'creation_to_observed_ready_seconds': ready['unix'] - creation if ready and creation else None, 'init_and_schedule_seconds': started - creation if started and creation else None, 'allocated_gpu_seconds_including_collection_idle': released['unix'] - scheduled if released and scheduled else None, 'donor_deleted_before_new_pod': bool(source_release and creation and source_release['unix'] <= creation), 'donor_delete_to_ready_seconds': ready['unix'] - source_release['unix'] if ready and source_release else None, 'container_to_first_valid_artifact_including_operator_gap_seconds': first_valid - started if first_valid and started else None, 'donor_delete_to_first_valid_artifact_seconds': first_valid - source_release['unix'] if first_valid and source_release else None, 'capture_wall_seconds': capture['completed_unix'] - capture['started_unix'] if capture else None, 'capture_receipt': first_json(raw / 'capture-stdout.json'), 'actual_cuda_criu_restore': '"mechanism": "cuda-criu-restored"' in worker, 'normal_load_fallback_observed': 'normal-load' in worker and 'intentional-incompatible' in worker, 'requests': attempts, 'runtime_graph_states': runtime_states, 'features': read(raw / 'features.json'), 'init_container_states': pod['status'].get('initContainerStatuses') if pod else None, 'image': value['spec']['containers'][0]['image'], 'node': value['spec']['nodeSelector']['kubernetes.io/hostname']})

for trial in trials:
    path = ROOT / 'raw' / trial['pod'] / 'supervisor.log'
    log = path.read_text() if path.exists() else ''
    for start in re.finditer(r'(?m)^\{', log):
        try:
            receipt = json.JSONDecoder().raw_decode(log[start.start():])[0]
        except ValueError:
            continue
        if receipt.get('action') == 'restore':
            trial['restore_receipt'] = receipt
    events = read(ROOT / 'raw' / trial['pod'] / 'events.json', {})
    trial['image_pull_events'] = [e['message'] for e in events.get('items', []) if e.get('reason') == 'Pulled']
    worker_path = ROOT / 'raw' / trial['pod'] / 'worker.log'
    worker_log = worker_path.read_text() if worker_path.exists() else ''
    trial['cuda_illegal_memory_access_observed'] = 'an illegal memory access was encountered' in worker_log
    if trial['cohort'] in ('restore', 'normal') and trial['cuda_illegal_memory_access_observed']:
        measured = [r for r in trial['requests'] if r['schedule'] == 'measured']
        for index, request in enumerate(measured):
            if not request.get('valid'):
                request['failure_interpretation'] = ('First measured reused-key inference failure after successful warmup; root cause unresolved, async CUDA stack may not identify originating kernel.' if trial['cohort'] == 'normal' else 'First restored inference failure on donor-warmed key; root cause unresolved, async CUDA stack may not identify originating kernel.') if index == 0 else 'Follow-on request in poisoned CUDA context; not an independent failure replicate.'

restores = [v for v in trials if v['cohort'] == 'restore' and v['ready'] and v['actual_cuda_criu_restore']]
normals = [v for v in trials if v['cohort'] == 'normal' and v['ready']]
fallbacks = [v for v in trials if v['cohort'] == 'fallback']
paired = []
for restored in restores:
    for normal in normals:
        for case in sorted(set(artifacts[restored['pod']]) & set(artifacts[normal['pod']])):
            r, n = artifacts[restored['pod']][case], artifacts[normal['pod']][case]
            result = metrics(coordinates(parse(n['structure'], n['format']), cases[case]), coordinates(parse(r['structure'], r['format']), cases[case]))
            paired.append({'restore': restored['pod'], 'normal': normal['pod'], 'case': case, 'structure_text_exact': r['structure'] == n['structure'], **result})
valid = sum(r.get('valid', False) for t in trials for r in t['requests'])
allocated = sum(t['allocated_gpu_seconds_including_collection_idle'] or 0 for t in trials)
rt = stat([t['container_to_observed_ready_seconds'] for t in restores])
nt = stat([t['container_to_observed_ready_seconds'] for t in normals])
ratio = nt['median'] / rt['median'] if nt and rt else None
serving_passed = [t for t in restores if sum(r.get('valid', False) for r in t['requests'] if r['schedule'] == 'measured') == 3]
complete = len(restores) == len(normals) == 3 and len(fallbacks) == 1 and all(t['released'] for t in trials) and all(len([r for r in t['requests'] if r['schedule'] == 'measured']) == 3 for t in restores + normals) and all(len(t['requests']) >= 3 for t in fallbacks)
output = {'model_id': 'openfold3', 'status': 'measured' if complete else 'in_progress', 'variant': 'Preview2 exact P2 checkpoint; public BIR0.1.0 FP32 native RNG, diffusion CUDA Graph, resident inner model', 'baseline': {'normal_trials': len(normals), 'container_to_observed_ready_seconds': nt, 'same_physical_gpu_and_harness': True}, 'comparisons': {'restore_trials': len(restores), 'container_to_observed_ready_seconds': rt, 'normal_over_restore_startup_ratio': ratio, 'not_current_vs_bir_speedup': True}, 'quality': {'valid_requests_all_schedules': valid, 'paired_structural_metrics': paired, 'limits': 'Small fixed-seed corpus; not broad accuracy certification. Same-seed timing repeats are not independent scientific replicates.'}, 'snapshot': {'fresh_donor_deleted': bool(restores) and all(t['donor_deleted_before_new_pod'] for t in restores), 'actual_restore_verified': bool(restores) and all(t['actual_cuda_criu_restore'] for t in restores), 'fallback_verified': bool(fallbacks) and all(t['normal_load_fallback_observed'] and sum(r.get('valid', False) for r in t['requests']) >= 3 for t in fallbacks), 'bundle_inventory': read(ROOT / 'inventory/bundle.json'), 'cuda_graph_evidence': 'Per-response runtime GRAPH_VERIFIED states retained for same and different shapes; no graph-off snapshot cohort.'}, 'resource_cost': {'allocated_gpu_seconds_all_trials': allocated, 'gpu_seconds_per_valid_request': allocated / valid if valid else None}, 'trials': trials, 'cleanup': read(ROOT / 'lifecycle/final-cleanup.json'), 'recommendation': 'Pending matched lifecycle evidence.' if not complete else ('Snapshot compatibility demonstrated but normal loading is faster; no startup adoption.' if ratio < 1 else 'Snapshot startup benefit measured for this exact binding; further operational qualification required.'), 'limitations': ['Same physical H100 and driver only, no GPU migration/remapping.', 'Readiness polling roughly3s plus API latency; captured load_seconds is not restore latency.', 'Normal and restore use identical container capabilities and supervisor, isolated from production.', 'Pinned app/dependency init work is repeated before both normal and restore; container and pod clocks are separate.', 'No automatic promotion, production routing changes, host PID namespace or host policy changes.']}
output['bir_fit'] = 'Exact public Preview2 model-seam candidate, not OpenBind; snapshot compatibility is independently measured.'
output['status'] = 'measured' if complete and len(serving_passed) == 3 else 'measured_negative' if complete else 'in_progress'
output['snapshot']['application_serving_qualified'] = complete and len(serving_passed) == 3
output['snapshot']['application_serving_passed_restore_trials'] = len(serving_passed)
output['snapshot']['low_level_restore_successes'] = len(restores)
output['snapshot']['output_parity_established'] = bool(paired) and len(serving_passed) == 3
output['snapshot']['startup_benefit_established'] = complete and len(serving_passed) == 3 and bool(ratio and ratio > 1)
output['snapshot']['cross_node_or_device_portability_tested'] = False
output['snapshot']['fallback_scope'] = 'Identity mismatch triggers ordinary model loading. Three distinct query keys are tested without prior warmup; this does not qualify same-key graph reuse or repair the normal-control failure.'
output['snapshot']['cuda_graph_evidence'] = 'Donor graph verified. After-restore graph usability requires successful real requests; failed requests do not establish replay/recapture. No graph-off snapshot cohort.'
output['comparisons']['readiness_is_not_valid_inference'] = len(serving_passed) != len(restores)
output['comparisons']['valid_result_startup_ratio'] = None if len(serving_passed) != len(restores) else ratio
output['resource_cost']['by_cohort'] = {}
for cohort in ('donor', 'restore', 'normal', 'fallback'):
    selected = [t for t in trials if t['cohort'] == cohort]
    seconds = sum(t['allocated_gpu_seconds_including_collection_idle'] or 0 for t in selected)
    good = sum(r.get('valid', False) for t in selected for r in t['requests'])
    attempts = sum(len(t['requests']) for t in selected)
    output['resource_cost']['by_cohort'][cohort] = {'allocated_gpu_seconds': seconds, 'attempts': attempts, 'valid': good, 'invalid': attempts - good, 'gpu_seconds_per_valid_request': seconds / good if good else None}
output['quality']['independent_first_inference_cuda_faults'] = sum(t['cuda_illegal_memory_access_observed'] for t in restores)
output['quality']['poisoned_context_follow_on_failures'] = sum(1 for t in restores for r in t['requests'] if r.get('failure_interpretation', '').startswith('Follow-on'))
output['quality']['normal_worker_repeated_key_cuda_faults'] = sum(t['cuda_illegal_memory_access_observed'] for t in normals)
output['quality']['normal_worker_poisoned_follow_ons'] = sum(1 for t in normals for r in t['requests'] if r.get('failure_interpretation', '').startswith('Follow-on'))
output['quality']['normal_warmups_valid'] = sum(r.get('valid', False) for t in normals for r in t['requests'] if r['schedule'] == 'warmup')
output['quality']['normal_measured_requests_valid'] = sum(r.get('valid', False) for t in normals for r in t['requests'] if r['schedule'] == 'measured')
if complete and len(serving_passed) != 3:
    output['recommendation'] = 'Reject process snapshots for this exact graph/resident/driver configuration: low-level CUDA+CRIU restore and readiness succeeded but real inference failed. Use normal loading. Root cause unresolved; no graph invalidation/reset variant tested.'
if any(t['cuda_illegal_memory_access_observed'] for t in normals):
    output['recommendation'] = 'HOLD graph serving and snapshots for this exact integration. Ordinary model loading also fails when the warmed query key is reused, so failures cannot be attributed to restoration alone. Distinct-key speed gains are performance potential, not deployment qualification. Graph cache lifetime/invalidation remedies are unimplemented and unmeasured.'
output['features'] = {'same_and_changed_shapes': ['1crn46', '1lyz129', 'T1031 full-MSA95'], 'native_http_contract_preserved': True, 'negative_contract_probe_evidence': 'raw/fs2-bioir-of3-fallback/features.json', 'seed': 'Native fixed42, no override API; same request schedule for normal and restored', 'batch_cancellation_idempotency': 'Not advertised or qualified by this native wrapper.'}
if output['cleanup']:
    output['cleanup']['gpu_memory_audit'] = read(ROOT / 'lifecycle/final-gpu-memory.json')
output['evidence'] = ['report.md', 'manifests/', 'raw/', 'lifecycle/', 'inventory/source-hashes.json']
output['limitations'].append('Fixed64GiB compute-csi-default-sc PVC. Donor/normal scratch is PVC-backed; restore scratch is fresh emptyDir copied from a read-only bundle. No storage changes or additional tuning within the cohort.')
output['limitations'].append('No new native Preview2 snapshot cohort was run. Historical native snapshot success is not a matched contemporary comparator and does not establish which BIR/graph/framework/driver component caused this failure.')
(ROOT / 'result.json').write_text(json.dumps({'schema': 'fs2-bioir-snapshot/v1', 'state': 'complete' if complete else 'running', 'models': [output], 'harness_failures': read(ROOT / 'inventory/harness-failures.json', [])}, indent=2) + '\n')
report = ROOT / 'report.md'
intro = report.read_text().split('\n## Observed results\n')[0]
lines = ['', '## Observed results', '', f'Low-level CUDA+CRIU restores reaching readiness: {len(restores)}; application-serving passes: {len(serving_passed)}; matched normal controls: {len(normals)}. Artifact-valid requests across all schedules: {valid}. Readiness is not accepted as successful inference.', '', '| Pod | Cohort | Container → ready (s) | Container → first valid artifact (s, includes orchestration gap) | Valid requests | CUDA+CRIU restore |', '| --- | --- | ---: | ---: | ---: | --- |']
def fmt(value):
    return f'{value:.3f}' if value is not None else 'unmeasured'
for trial in trials:
    lines.append(f'| {trial["pod"]} | {trial["cohort"]} | {fmt(trial["container_to_observed_ready_seconds"])} | {fmt(trial["container_to_first_valid_artifact_including_operator_gap_seconds"])} | {sum(r.get("valid", False) for r in trial["requests"])} | {trial["actual_cuda_criu_restore"]} |')
if rt and nt:
    lines += ['', f'Median container-to-ready: restore {rt["median"]:.3f}s, normal {nt["median"]:.3f}s; normal/restore ratio {ratio:.3f}x. Individual trials, ranges, init states, pull events and restore subphase receipts are in result.json.']
    lines += ['', 'Startup benefit is not established: no restored request produced a valid artifact, and even readiness was slower. A valid-result startup speedup is undefined.'] if not serving_passed else []
if paired:
    lines += ['', f'Paired normal/restored comparisons: {len(paired)}; exact structure text in {sum(v["structure_text_exact"] for v in paired)}. Minimum CA-lDDT {min(v["ca_lddt"] for v in paired):.6f}; maximum aligned CA RMSD {max(v["ca_rmsd_angstrom"] for v in paired):.6f} A. These are repeated small-shape controls, not independent accuracy samples.']
elif restores:
    lines += ['', 'No restored valid structures are available for paired output equivalence. CUDA illegal-memory-access errors on the first restored inference are distinguished from subsequent poisoned-context errors; three failed shapes in one process are not three independent CUDA fault replicates. Low-level restore success does not qualify this application.']
if any(t['cuda_illegal_memory_access_observed'] for t in normals):
    lines += ['', 'Critical normal-control result: normal warmup succeeds, then the same query key fails with CUDA illegal memory access and subsequent requests see a poisoned context. This reproduces without any process restoration. The core graph benchmark varied query IDs and therefore recaptured graphs; its speed gains do not qualify cached-graph reuse. The originating kernel and exact lifetime bug remain unresolved because CUDA errors are asynchronous. No graph invalidation/reset or new runtime variant was tested.']
if fallbacks and fallbacks[0]['released']:
    fallback = fallbacks[0]
    lines += ['', f'Incompatible-identity normal-load fallback observed: {fallback["normal_load_fallback_observed"]}; valid requests {sum(r.get("valid", False) for r in fallback["requests"])}/{len(fallback["requests"])}. These are three distinct query keys without prior warmup, not validation of cached-key reuse. Negative contract-probe results are retained separately in the fallback raw evidence.']
if complete:
    counts = output['resource_cost']['by_cohort']
    lines += ['', f'All inference schedules: {sum(v["attempts"] for v in counts.values())} attempts, {valid} valid artifacts, {sum(v["invalid"] for v in counts.values())} HTTP500 failures. The three restored first-request faults and three ordinary-load repeated-key faults are six independent process-level events; their twelve follow-on failures are poisoned-context consequences. The seven deliberately invalid feature probes are separate from inference counts.']
captures = [t for t in trials if t['capture_receipt']]
for trial in captures:
    receipt = trial['capture_receipt']
    lines += ['', f'Capture {trial["pod"]}: status {receipt["status"]}, complete capture wall {fmt(trial["capture_wall_seconds"])}s, durable flush {fmt(receipt.get("checkpoint_flush_seconds"))}s. CUDA/CRIU command times remain separate in the receipt.']
lines += ['', f'Whole-trial GPU allocation: {allocated:.3f}s, including initialization, capture, all failures, collection and operator/orchestration gaps. This is not steady-state per-request production cost.', '', 'Decision: ' + output['recommendation'], '', 'Cross-node/device portability is untested. No result is transferred to another GPU UUID, driver, checkpoint, precision or model variant.']
if output['snapshot']['bundle_inventory']:
    bundle = output['snapshot']['bundle_inventory']
    lines += ['', f'Recorded bundle/PVC file inventory: {bundle["total_file_bytes"]} bytes across {len(bundle["files"])} files. Individual sizes and SHA256 fingerprints are retained before cleanup.']
if output['cleanup']:
    lines += ['', 'Resource disposition: ' + output['cleanup'].get('note', 'See final-cleanup.json for exact names and completed actions.')]
    if output['cleanup'].get('gpu_memory_audit'):
        lines += ['', 'Final read-only GPU audit: ' + '; '.join(f'{g["name"]}, {g["memory.used"]} MiB used, {g["utilization.gpu"]}% utilization' for g in output['cleanup']['gpu_memory_audit']['gpus']) + '. Scheduler allocations and compute processes are empty.']
report.write_text(intro + '\n'.join(lines) + '\n')
print(json.dumps({'status': output['status'], 'restores': len(restores), 'normals': len(normals), 'valid': valid, 'ratio': ratio}))
