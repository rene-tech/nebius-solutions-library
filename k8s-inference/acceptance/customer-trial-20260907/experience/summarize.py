"""Summarize retained trial observations without hiding failed samples or retries."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def statistics_for(rows):
    durations = sorted(row['client_seconds'] for row in rows)
    p95 = None
    if len(durations) >= 20:
        position = (len(durations) - 1) * 0.95
        lower = int(position)
        p95 = durations[lower] + (durations[min(lower + 1, len(durations) - 1)] - durations[lower]) * (position - lower)
    return {'samples': len(rows), 'passed': sum(row['status'] == 'passed' for row in rows),
            'failed': sum(row['status'] != 'passed' for row in rows),
            'median_seconds': statistics.median(durations) if durations else None,
            'min_seconds': min(durations) if durations else None,
            'max_seconds': max(durations) if durations else None,
            'p95_seconds': p95,
            'p95_note': 'Linear empirical percentile, descriptive only.' if p95 is not None else 'Not reported with fewer than20 observations.'}


def build(raw, *, complete=False):
    first_path, second_path = raw / 'interactive.jsonl', raw / 'r02/interactive.jsonl'
    first, second = read_lines(first_path), read_lines(second_path)
    rows = first + second
    ordinals = [row['ordinal'] for row in rows]
    if ordinals != list(range(1, len(rows) + 1)):
        raise ValueError('Observation sequence has missing, repeated or reordered samples')
    if any(row.get('submission_attempts') != 1 for row in rows):
        raise ValueError('Unexpected inference retry in traffic sampler')
    stop = raw / 'r02/stop.json'
    if complete and not stop.exists():
        raise ValueError('Sampler still active; complete report requires the explicit final stop record')
    discovery = read_lines(raw / 'discovery.jsonl') + read_lines(raw / 'r02/discovery.jsonl')
    endpoint_checks = {}
    for page in discovery:
        for row in page['checks']:
            key = row['path']
            bucket = endpoint_checks.setdefault(key, {'checks': 0, 'http_status_counts': {}, 'model_id_sets': []})
            bucket['checks'] += 1
            status = str(row.get('http_status', 'transport-error'))
            bucket['http_status_counts'][status] = bucket['http_status_counts'].get(status, 0) + 1
            if 'model_ids' in row and row['model_ids'] not in bucket['model_id_sets']:
                bucket['model_id_sets'].append(row['model_ids'])
    handoff = None
    if first and second:
        handoff = {'previous_ordinal': first[-1]['ordinal'], 'next_ordinal': second[0]['ordinal'],
                   'last_r01_started_at': first[-1]['started_at'], 'last_r01_completed_at': first[-1]['completed_at'],
                   'first_r02_started_at': second[0]['started_at'],
                   'start_to_start_seconds': (timestamp(second[0]['started_at']) - timestamp(first[-1]['started_at'])).total_seconds(),
                   'idle_between_completed_and_next_start_seconds': (timestamp(second[0]['started_at']) - timestamp(first[-1]['completed_at'])).total_seconds(),
                   'reason': 'Approved graceful monitoring-route correction. Next ordinal and HTTP/MCP alternation preserved; no inference was retried.'}
    fields = ('ordinal', 'surface', 'case', 'phase', 'started_at', 'completed_at', 'client_seconds',
              'request_sha256', 'response_sha256', 'expected_output', 'actual_output', 'status',
              'failure', 'failure_http_status', 'correctness_passed', 'operation_id', 'submission_attempts', 'status_polls', 'usage')
    projected = [{**{key: row[key] for key in fields if key in row},
                  'operation_status': row.get('operation', {}).get('status'),
                  'model_revision': row.get('operation', {}).get('model_revision'),
                  'runtime': row.get('operation', {}).get('runtime'),
                  'server_attempt': row.get('operation', {}).get('attempt')} for row in rows]
    browser_files = sorted((raw / 'browser').glob('*.json'))
    browser_evidence = [{'name': path.name, 'sha256': sha(path), 'observed_at': json.loads(path.read_text())['at'],
                         'screenshot_sha256': sha(raw / 'browser/output/playwright' / (path.stem + '.png'))}
                        for path in browser_files if (raw / 'browser/output/playwright' / (path.stem + '.png')).exists()]
    evidence = {'schema': 'fs2-serve.nebius.ai/customer-trial-experience/v1',
                'status': 'complete' if complete else 'in-progress', 'exported_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'One synthetic short Qwen request every25seconds, one client, HTTP/MCP alternating alongside the separate scientific campaign. '
                         'Known valid fixtures; not unaided onboarding, scientific validation, stress/load capacity or a production SLA.',
                'clock': 'End-to-end non-streaming public request, including network/session setup, admission, status polling and complete response validation. '
                         'Not TTFT, isolated decode throughput or model cold start.',
                'credential_scope': {'http_qwen': 'existing inference key', 'mcp_qwen': 'existing MCP inference key',
                                     'science_discovery_and_mcp_readback': 'existing academic scientific key',
                                     'browser': 'Existing bootstrap administrator operator session; not a customer-scoped login.'},
                'total': statistics_for(rows),
                'by_surface': {surface: statistics_for([row for row in rows if row['surface'] == surface])
                               for surface in ('http', 'mcp')},
                'successful_response_latency': statistics_for([row for row in rows if row['status'] == 'passed']),
                'latency_population': 'Total and phase/surface distributions include failed requests through their failure boundary; successful_response_latency is explicitly separate.',
                'by_phase': {phase: statistics_for([row for row in rows if row['phase'] == phase]) for phase in ('baseline', 'during', 'after')},
                'by_phase_surface': {phase: {surface: statistics_for([row for row in rows if row['phase'] == phase and row['surface'] == surface])
                                             for surface in ('http', 'mcp')} for phase in ('baseline', 'during', 'after')},
                'requests': projected, 'discovery_checks': endpoint_checks, 'handoff': handoff,
                'browser_evidence': browser_evidence,
                'harness_caveats': [
                    'r01 requested nonexistent public /healthz; every404 is retained and excluded from platform availability. r02 uses verified public /readyz.',
                    'r01 used academic scope for general model discovery and correctly saw only msa-search-pdb70. r02 uses general scope and sees14 serving models; scientific discovery uses academic scope and sees10.',
                    'One manual Protenix URL contained an incorrect UUID; browser returned404 twice (frontend GET retry). Correct receipt-derived UUID succeeded. This is a harness navigation error, not a failed real operation.',
                    'Initial unauthenticated browser session lookup401 is the expected sign-in exchange; no inference401 was hidden.',
                    'CLI VM could not read credentials in memory (require/import unavailable); an in-memory Playwright session was used without writing a cookie/session file.',
                    'One-time /livez route discovery also returned404 before selecting /readyz; it did not submit an inference request.',
                ],
                'raw_receipts': [{'name': str(path.relative_to(raw)), 'sha256': sha(path)}
                                 for path in (first_path, second_path, raw / 'discovery.jsonl', raw / 'r02/discovery.jsonl',
                                              raw / 'interactive_sampler-r01.py', raw / 'r02/harness.json',
                                              raw / 'phase.json', stop) if path.exists()]}
    mcp = raw / 'scientific-mcp-readback-r01.json'
    if mcp.exists():
        readback = json.loads(mcp.read_text())
        evidence['scientific_mcp_readback'] = {key: readback[key] for key in ('scope', 'started_at', 'completed_at', 'protocol', 'status', 'calls')}
        evidence['scientific_mcp_readback']['private_receipt_sha256'] = sha(mcp)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--complete', action='store_true')
    args = parser.parse_args()
    evidence = build(args.raw, complete=args.complete)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'status': evidence['status'], 'total': evidence['total'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()
