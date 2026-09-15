#!/usr/bin/env python3
"""Freeze current deployment and render an isolated exact-image benchmark pod."""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
KUBECONFIG = '/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig'
NAMESPACE = 'fs2-bioir-openfold'
NODES = {'h100': 'computeinstance-e00j20a9hkb508cn4a', 'l40s': 'computeinstance-e00dczh75qcnbj0bx8'}
DEPLOYMENTS = {'openfold2': 'openfold2-b300-hot-h100-reserved-8x', 'openfold3': 'openfold3-hot-h100-reserved-8x'}

def kubectl(*args):
    return subprocess.check_output(['kubectl', '--kubeconfig', KUBECONFIG, *args], text=True)

def save(name, data):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model', choices=DEPLOYMENTS)
    parser.add_argument('--gpu', choices=NODES, default='h100')
    args = parser.parse_args()
    deployment = json.loads(kubectl('-n', 'fs2-models', 'get', 'deployment', DEPLOYMENTS[args.model], '-o', 'json'))
    pods = json.loads(kubectl('get', 'pods', '-A', '-o', 'json'))['items']
    allocated = [p for p in pods if p['spec'].get('nodeName') == NODES[args.gpu] and p['status']['phase'] not in ('Succeeded', 'Failed') and any(int(c.get('resources', {}).get('requests', {}).get('nvidia.com/gpu', 0)) for c in p['spec']['containers'])]
    if allocated:
        raise SystemExit('Assigned GPU already allocated: ' + ', '.join(p['metadata']['name'] for p in allocated))
    node = json.loads(kubectl('get', 'node', NODES[args.gpu], '-o', 'json'))
    save(f'raw/{args.model}-{args.gpu}-deployment.json', deployment)
    save(f'raw/{args.model}-{args.gpu}-node.json', node)
    save(f'raw/{args.model}-{args.gpu}-preflight.json', {'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(), 'active_gpu_pods': [], 'pending_pods': [{'namespace': p['metadata']['namespace'], 'name': p['metadata']['name'], 'resources': [c.get('resources', {}) for c in p['spec']['containers']]} for p in pods if p['status']['phase'] == 'Pending']})
    spec = copy.deepcopy(deployment['spec']['template']['spec'])
    spec.pop('serviceAccount', None)
    spec.pop('serviceAccountName', None)
    spec['nodeSelector'] = {'kubernetes.io/hostname': NODES[args.gpu]}
    spec['restartPolicy'] = 'Never'
    spec['terminationGracePeriodSeconds'] = 30
    # Preserve entrypoint, runtime environment and model. Isolate writable compile cache.
    for volume in spec['volumes']:
        if 'persistentVolumeClaim' in volume:
            volume.pop('persistentVolumeClaim')
            volume['emptyDir'] = {'sizeLimit': '12Gi'}
    for container in spec['containers']:
        container.pop('livenessProbe', None)
        container.pop('startupProbe', None)
        container['resources']['requests']['ephemeral-storage'] = '20Gi'
        container['resources']['limits']['ephemeral-storage'] = '60Gi'
        if args.gpu == 'l40s':
            # The assigned L40S node has ~57 GiB host RAM, below the H100
            # deployment's reservation. Apply the same host cap to both A/B.
            container['resources']['requests']['memory'] = '32Gi'
            container['resources']['limits']['memory'] = '48Gi'
    labels = {'evaluation': 'fs2-bioir-20260915', 'lane': 'openfold', 'model': args.model, 'variant': 'baseline'}
    name = f'{args.model}-baseline-{args.gpu}'
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': labels}}
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NAMESPACE, 'labels': labels}, 'spec': spec}
    save(f'manifests/{name}.json', {'apiVersion': 'v1', 'kind': 'List', 'items': [namespace, pod]})
    print(ROOT / f'manifests/{name}.json')

if __name__ == '__main__':
    main()
