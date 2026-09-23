"""Evict one qualification Pod after a committed MD checkpoint, never its node.

The existing customer client continues polling. All checks bind the exact
GROMACS operation, first attempt, Job UID, Pod UID and immutable worker image.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

IMAGE = 'cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:4636ae471eda7faee35cee1bf804e85f5c9635f94f99baad11c564c25516e123'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('This fault injection has already been attempted; use its retained receipt.')
    kube = ['kubectl', '--kubeconfig', args.kubeconfig, '--context', args.context, '-n', 'fs2-models']
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status_file = args.receipt / 'status.json'
        if not status_file.exists():
            time.sleep(3)
            continue
        try:
            status = json.loads(status_file.read_text())
        except json.JSONDecodeError:
            continue
        operation = status['operation']
        assert operation['model_id'] == 'gromacs'
        assert operation['tenant_id'] == operation['principal_id'] == 'rene'
        assert operation['idempotency_key'] == 'gromacs-qualified-recovery-large-20260923-01'
        assert operation['token_id'] == '6f4b312e-cdf1-480d-9e37-bc31c9b952fd'
        if operation['status'] in {'failed', 'cancelled', 'succeeded'}:
            raise RuntimeError('Operation ended before the controlled disruption.')
        attempts = status['batch']['stages'][0]['attempts']
        if not attempts:
            time.sleep(3)
            continue
        attempt = attempts[0]
        assert attempt['attempt_number'] == 1 and attempt['shard_id'] == 'throughput-01'
        pods = json.loads(subprocess.check_output(kube + ['get', 'pods', '-l',
            f'batch.kubernetes.io/job-name={attempt["workload_name"]}', '-o', 'json']))['items']
        if len(pods) != 1 or pods[0]['status']['phase'] != 'Running':
            time.sleep(3)
            continue
        pod = pods[0]
        assert any(owner['uid'] == attempt['workload_uid'] for owner in pod['metadata']['ownerReferences'])
        stage = next(container for container in pod['spec']['containers'] if container['name'] == 'scientific-stage')
        assert stage['image'] == IMAGE
        inspect = """import json,pathlib
items=[]
for ack in pathlib.Path('/mnt/fs2-scientific').rglob('checkpoint-ack.json'):
    value=json.loads(ack.read_text())
    state=json.loads((ack.parent/'gromacs-state.json').read_text())
    if value.get('status')=='committed' and value.get('generation',0)>=2:
        items.append({'ack':value,'state':state})
print(json.dumps(items))
"""
        result = subprocess.run(kube + ['exec', pod['metadata']['name'], '-c', 'scientific-stage', '--',
            'python3', '-c', inspect], capture_output=True)
        states = json.loads(result.stdout) if result.returncode == 0 else []
        states = [item for item in states if item['state']['operation_id'] == operation['id']
                  and item['state']['job_id'] == 'throughput-01'
                  and (item['state'].get('active_step') or {}).get('id') == 'production']
        if not states:
            time.sleep(3)
            continue
        receipt = {'operation_id': operation['id'], 'pod': pod['metadata']['name'],
                   'pod_uid': pod['metadata']['uid'], 'node': pod['spec']['nodeName'],
                   'first_attempt': attempt, 'checkpoint': states[0],
                   'requested_at': datetime.now(timezone.utc).isoformat(),
                   'scope': 'One task-owned Pod via Eviction API; no node or shared workload mutation.'}
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(receipt, output, indent=2)
        eviction = {'apiVersion': 'policy/v1', 'kind': 'Eviction',
                    'metadata': {'name': receipt['pod'], 'namespace': 'fs2-models'},
                    'deleteOptions': {'gracePeriodSeconds': 30, 'preconditions': {'uid': receipt['pod_uid']}}}
        subprocess.run(kube + ['create', '--raw',
            f'/api/v1/namespaces/fs2-models/pods/{receipt["pod"]}/eviction', '-f', '-'],
            input=json.dumps(eviction).encode(), check=True)
        print(json.dumps({'operation_id': operation['id'], 'pod': receipt['pod'],
                          'committed_generation': states[0]['ack']['generation'], 'eviction_requested': True}))
        return
    raise TimeoutError('No committed active MD checkpoint became available; nothing evicted.')


if __name__ == '__main__':
    main()
