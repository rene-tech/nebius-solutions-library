#!/usr/bin/env python3
"""Exact current OF3 image with only the explicitly paired GPU residency change."""
import argparse
import copy
import json
from prepare_baseline import ROOT, NODES, NAMESPACE, kubectl, save

p = argparse.ArgumentParser()
p.add_argument('--gpu', choices=NODES, default='h100')
a = p.parse_args()
name = f'openfold3-upstream-resident-{a.gpu}'
pods = json.loads(kubectl('get', 'pods', '-A', '-o', 'json'))['items']
allocated = [v for v in pods if v['spec'].get('nodeName') == NODES[a.gpu] and v['status']['phase'] not in ('Failed', 'Succeeded') and any(c.get('resources', {}).get('requests', {}).get('nvidia.com/gpu') for c in v['spec']['containers'])]
assert not allocated, 'Assigned GPU is allocated'
plugin = next(v for v in pods if v['spec'].get('nodeName') == NODES[a.gpu] and 'nvidia-device-plugin' in v['metadata']['name'])
processes = kubectl('-n', plugin['metadata']['namespace'], 'exec', plugin['metadata']['name'], '--', 'nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory', '--format=csv,noheader')
assert not processes.strip()
save('raw/' + name + '-preflight.json', {'gpu_processes': processes, 'active_gpu_pods': [], 'node': NODES[a.gpu]})
baseline = json.loads((ROOT / f'manifests/openfold3-baseline-{a.gpu}.json').read_text())
pod = copy.deepcopy(next(v for v in baseline['items'] if v['kind'] == 'Pod'))
pod['metadata']['name'] = name
pod['metadata']['labels']['variant'] = 'upstream-resident'
script_names = ['upstream_openfold3_resident_server.py', 'resident_gpu.py']
cm_name = 'openfold3-upstream-resident-source'
cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': cm_name, 'namespace': NAMESPACE, 'labels': pod['metadata']['labels']}, 'data': {n: (ROOT / n).read_text() for n in script_names}}
runtime = pod['spec']['containers'][0]
runtime['command'] = ['/bin/bash', '-ec', 'source /opt/fs2/activate.sh; export TRITON_CACHE_DIR=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/triton TORCH_EXTENSIONS_DIR=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/torch-extensions XDG_CACHE_HOME=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/xdg CUDA_CACHE_PATH=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/cuda; exec python /eval/upstream_openfold3_resident_server.py']
runtime.pop('args', None)
runtime['volumeMounts'].append({'name': 'evaluation-source', 'mountPath': '/eval', 'readOnly': True})
pod['spec']['volumes'].append({'name': 'evaluation-source', 'configMap': {'name': cm_name}})
save('manifests/' + name + '.json', {'apiVersion': 'v1', 'kind': 'List', 'items': [cm, pod]})
print(ROOT / ('manifests/' + name + '.json'))
