#!/usr/bin/env python3
"""Remove only frozen task-owned source ConfigMaps after GPU pod collection."""
import datetime
import json
from prepare_baseline import NAMESPACE, NODES, kubectl, save

assert not json.loads(kubectl('-n', NAMESPACE, 'get', 'pods', '-o', 'json'))['items']
targets = ['openfold2-bir-source', 'openfold3-bir-source', 'openfold3-upstream-resident-source']
receipts = []
for target in targets:
    item = json.loads(kubectl('-n', NAMESPACE, 'get', 'configmap', target, '-o', 'json'))
    assert item['metadata']['labels']['evaluation'] == 'fs2-bioir-20260915' and item['metadata']['labels']['lane'] == 'openfold'
    receipts.append({'kind': 'configmap', 'name': target, 'uid': item['metadata']['uid']})
    print(kubectl('-n', NAMESPACE, 'delete', 'configmap', target, '--wait=true'))
pods = json.loads(kubectl('get', 'pods', '-A', '-o', 'json'))['items']
nodes = []
for gpu, node in NODES.items():
    allocated = [p['metadata']['namespace'] + '/' + p['metadata']['name'] for p in pods if p['spec'].get('nodeName') == node and p['status']['phase'] not in ('Succeeded', 'Failed') and any(c.get('resources', {}).get('requests', {}).get('nvidia.com/gpu') for c in p['spec']['containers'])]
    plugin = next(p for p in pods if p['spec'].get('nodeName') == node and 'nvidia-device-plugin' in p['metadata']['name'])
    processes = kubectl('-n', plugin['metadata']['namespace'], 'exec', plugin['metadata']['name'], '--', 'nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory', '--format=csv,noheader')
    state = kubectl('-n', plugin['metadata']['namespace'], 'exec', plugin['metadata']['name'], '--', 'nvidia-smi', '--query-gpu=name,driver_version,memory.used,utilization.gpu', '--format=csv,noheader')
    assert not allocated and not processes.strip(), 'Verify before reassigning node to snapshot lane'
    nodes.append({'gpu': gpu, 'node': node, 'allocations': allocated, 'compute_processes': processes, 'state': state})
save('cleanup.json', {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'deleted': receipts, 'nodes': nodes, 'namespace_retained': True, 'pvc_created': False, 'production_changed': False, 'note': 'Only task-owned source ConfigMaps removed; exact source remains in frozen local manifests. Both GPU nodes free before snapshot reassignment.'})
