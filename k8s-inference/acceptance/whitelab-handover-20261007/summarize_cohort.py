"""Project retained QA receipts into public-safe, reproducible evidence.

Never copies credentials, presigned upload handles, inputs or raw transcripts.
Failed clients stay failed even if a separately recorded recovery later succeeds.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def seconds(start, end):
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3) if start and end else None


def summarize(root):
    completed = read(root / 'completed.json', [])
    samples = [json.loads(line) for line in (root / 'observations.jsonl').read_text().splitlines()]
    jobs = []
    for row in completed:
        directory = root / row['name']
        status = read(directory / 'status.json', {})
        operation = status.get('operation', {})
        admission = read(directory / 'admission.json', {})
        identity = operation or admission.get('operation', {})
        result = read(directory / 'result.json', {})
        worker = read(directory / 'worker-result.json', {})
        validation = read(directory / 'output-validation.json')
        request = read(directory / 'request.json', {})
        parameters = request.get('parameters', {})
        attempts = result.get('attempts', [])
        jobs.append({
            'name': row['name'], 'model': row['model'],
            'interface': 'mcp' if (directory / 'mcp-tool.json').exists() else 'rest',
            'operation_id': identity.get('id'), 'client_exit_code': row['client_exit_code'],
            'semantic_validation_exit_code': row.get('semantic_validation_exit_code'),
            'accepted_at': identity.get('accepted_at'), 'completed_at': operation.get('completed_at'),
            'accepted_to_completed_seconds': seconds(identity.get('accepted_at'), operation.get('completed_at')),
            'status': operation.get('status'), 'result_published': status.get('batch', {}).get('result_published'),
            'attempts': len(attempts),
            'pools': sorted({item.get('scheduling_admission', {}).get('resolved_pool_id')
                             for item in attempts if item.get('scheduling_admission', {}).get('resolved_pool_id')}),
            'execution_identity': result.get('execution_identity'),
            'worker_cells': worker.get('cells'), 'worker_genes': worker.get('genes'),
            'worker_elapsed_seconds': worker.get('elapsed_seconds'),
            'worker_timings_seconds': worker.get('timings_seconds'),
            'input_sha256': worker.get('input_sha256'),
            'worker_recipe_sha256': worker.get('recipe_sha256'),
            'worker_versions': worker.get('versions'),
            'resolved_parameters': {key: value for key, value in worker.get('parameters', {}).items()
                                    if key != 'output_prefix'},
            'peak_gpu_memory_bytes': worker.get('peak_gpu_memory_bytes'),
            'peak_host_rss_bytes': worker.get('peak_host_rss_bytes'),
            'gpu_snapshot_used': worker.get('gpu_snapshot_used'),
            'output_validation': validation,
            'request_sha256': hashlib.sha256((directory / 'request.json').read_bytes()).hexdigest(),
            'scientific_parameters': {key: parameters[key] for key in (
                'method', 'mode', 'resource_profile', 'counts_source', 'batch_key', 'labels_key',
                'gene_selection', 'n_top_genes', 'hvg_span', 'max_epochs', 'scanvi_max_epochs',
                'seed', 'batch_size', 'visualization', 'visualization_cells',
                'output_destination'
            ) if key in parameters},
        })
    failures = [row['name'] for row in completed if row['client_exit_code'] or row.get('semantic_validation_exit_code', 0)]
    unavailable = [{key: sample.get(key) for key in ('timestamp', 'http_status', 'observer_error')}
                   for sample in samples if sample.get('http_status') != 200]
    return {
        'schema': 'whitelab-public-cohort-evidence/v1', 'cohort': root.name,
        'expected_operations': 10, 'completed_client_operations': len(completed),
        'client_or_validation_failures': failures,
        'public_readiness_samples': len(samples), 'unavailable_samples': unavailable,
        'observed_running_worker_pods_max': max((sum(pod['phase'] == 'Running' for pod in sample.get('pods', [])) for sample in samples), default=0),
        'observed_nodes': sorted({pod['node'] for sample in samples for pod in sample.get('pods', []) if pod.get('node')}),
        'interfaces': dict(Counter(job['interface'] for job in jobs)),
        'clean_cohort': len(completed) == 10 and not failures and bool(samples) and not unavailable,
        'biological_accuracy_claimed': False, 'jobs': jobs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-clean', action='store_true')
    args = parser.parse_args()
    summary = summarize(args.evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    print(json.dumps({key: summary[key] for key in ('cohort', 'completed_client_operations', 'clean_cohort', 'client_or_validation_failures', 'public_readiness_samples')}))
    if args.require_clean and not summary['clean_cohort']:
        raise SystemExit('Not a clean cohort; retain evidence and investigate rather than relabel it')


if __name__ == '__main__':
    main()
