#!/usr/bin/env python3
"""Aggregate retained requests and bounded whole-study GPU allocations."""
import datetime as dt
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
def stamp(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
def stats(values):
    return {'n': len(values), 'median_seconds': statistics.median(values), 'min_seconds': min(values), 'max_seconds': max(values), 'individual_seconds': values} if values else None
resources = []
for path in sorted((ROOT / 'raw').iterdir()):
    if not path.is_dir() or not (path / 'pod.json').exists() or not (path / 'collected.json').exists():
        continue
    pod = json.loads((path / 'pod.json').read_text())
    rows = [json.loads(s) for s in (path / 'attempts.jsonl').read_text().splitlines()] if (path / 'attempts.jsonl').exists() else []
    conditions = {c['type']: c for c in pod['status'].get('conditions', [])}
    start = conditions.get('PodScheduled', {}).get('lastTransitionTime', pod['metadata']['creationTimestamp'])
    end = json.loads((path / 'collected.json').read_text())['utc']
    allocation = {'start_scheduled_utc': start, 'evidence_collected_utc': end, 'lower_seconds': stamp(end) - stamp(start), 'upper_seconds': None}
    if (path / 'deletion.json').exists():
        deleted = json.loads((path / 'deletion.json').read_text())
        allocation.update({'delete_requested_utc': deleted['requested_at'], 'deleted_observed_utc': deleted['deletion_observed_at'], 'lower_seconds': stamp(deleted['requested_at']) - stamp(start), 'upper_seconds': stamp(deleted['deletion_observed_at']) - stamp(start)})
    warm = {case: stats([r['wall_to_validated_artifact_seconds'] for r in rows if r['case'] == case and r['cohort'] == 'warm' and r.get('valid')]) for case in sorted({r['case'] for r in rows})}
    events = json.loads((path / 'events.json').read_text()) if (path / 'events.json').exists() else {}
    valid = sum(r.get('valid', False) for r in rows)
    probes = json.loads((path / 'feature-probes.json').read_text()) if (path / 'feature-probes.json').exists() else []
    item = {'resource': path.name, 'model': path.name.split('-')[0], 'gpu': path.name.split('-')[-1], 'node': pod['spec'].get('nodeName'), 'images': [c['image'] for c in pod['spec']['containers']], 'attempts': len(rows), 'valid': valid, 'invalid': len(rows) - valid, 'startup_failed_without_requests': not rows, 'warm': warm, 'first_shape': [r for r in rows if r['cohort'] == 'first_shape'], 'gpu_allocation': allocation, 'image_pull_events': [e['message'] for e in events.get('items', []) if e.get('reason') == 'Pulled'], 'container_states': pod['status'].get('containerStatuses'), 'ready_transition_utc': conditions.get('Ready', {}).get('lastTransitionTime'), 'mixed': json.loads((path / 'mixed-summary.json').read_text()) if (path / 'mixed-summary.json').exists() else None}
    item['negative_contract_probes'] = {'attempts': len(probes), 'expected_400_rejections': sum(p['http_status'] == 400 for p in probes), 'unexpected_statuses': [p for p in probes if p['http_status'] != 400], 'meaning': 'Deliberately invalid feature requests, separate from valid-inference denominator.'}
    if rows:
        item['accepted_to_first_basic_valid_artifact_seconds'] = rows[0]['wall_to_validated_artifact_seconds'] if rows[0].get('valid') else None
        item['scheduled_to_first_basic_valid_artifact_seconds_including_operator_delay'] = stamp(rows[0]['utc']) + rows[0]['wall_to_validated_artifact_seconds'] - stamp(start) if rows[0].get('valid') else None
        item['sum_native_locked_inference_seconds'] = sum(r.get('validation', {}).get('server_seconds', 0) for r in rows)
        item['warm_native_locked_inference'] = {case: stats([r['validation']['server_seconds'] for r in rows if r['case'] == case and r['cohort'] == 'warm' and r.get('valid')]) for case in warm}
    resources.append(item)
for row in resources:
    allocation = row['gpu_allocation']
    if allocation['upper_seconds'] is None:
        following = [stamp(r['gpu_allocation']['start_scheduled_utc']) for r in resources if r['node'] == row['node'] and stamp(r['gpu_allocation']['start_scheduled_utc']) > stamp(allocation['evidence_collected_utc'])]
        if following:
            allocation['upper_seconds'] = min(following) - stamp(allocation['start_scheduled_utc'])
            allocation['upper_bound_source'] = 'Next exclusive task GPU pod scheduled; exact earlier deletion time unavailable.'
    if row['valid']:
        allocation['gpu_seconds_per_valid_request_lower'] = allocation['lower_seconds'] / row['valid']
        allocation['gpu_seconds_per_valid_request_upper'] = allocation['upper_seconds'] / row['valid'] if allocation['upper_seconds'] is not None else None
        allocation['valid_per_allocated_gpu_hour_lower'] = row['valid'] * 3600 / allocation['upper_seconds'] if allocation['upper_seconds'] else None
        allocation['valid_per_allocated_gpu_hour_upper'] = row['valid'] * 3600 / allocation['lower_seconds']
output = {'timing_boundary': 'In-pod accepted HTTP through full response, artifact write and basic validation; no gateway, scheduler or offline structural-quality scoring. Server inference_seconds is whole native locked inference, not GPU-forward time.', 'sample_warning': 'Three warm repeats per case. Report individual values and median/range; not a tail-SLO estimate.', 'allocation_warning': 'Whole experiment reserved GPU includes image pull, initialization, failures, operator idle, collection and termination. Bounds are not inference compute. No GPU price assumed. No valid result means per-valid cost undefined, not zero.', 'resources': resources}
(ROOT / 'measurements.json').write_text(json.dumps(output, indent=2) + '\n')
print(json.dumps({'resources': len(resources), 'attempts': sum(r['attempts'] for r in resources), 'valid': sum(r['valid'] for r in resources)}))
