#!/usr/bin/env python3
"""Own OpenFold3 snapshot namespace objects, cloning the frozen benchmark worker."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('shared_snapshot_control', ROOT.parent / 'control.py')
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
K, K8S, ACCEPTANCE, TOOLS = shared.K, shared.K8S, shared.ACCEPTANCE, shared.TOOLS
NS = 'fs2-bioir-snapshot'
NODE = 'computeinstance-e00j20a9hkb508cn4a'
PVC = 'fs2-bioir-of3-checkpoints'
LABELS = {'evaluation': 'fs2-bioir-20260915', 'lane': 'snapshot', 'benchmark-model': 'openfold3-bir'}
shared.ROOT, shared.NS, shared.NODE, shared.PVC, shared.LABELS = ROOT, NS, NODE, PVC, LABELS
save = shared.save

def k(*args, **kwargs):
    return shared.k(*args, **kwargs)

def apply(name, value):
    # Full A3M is larger than client-side apply's annotation limit, although
    # well inside the ConfigMap data limit. Create the immutable object once.
    if value['kind'] == 'ConfigMap' and value['metadata']['name'] == 'fs2-bioir-of3-snapshot-app':
        save('manifests/' + name + '.json', value)
        path = str(ROOT / 'manifests' / (name + '.json'))
        print(k('create', '--dry-run=client', '-f', path), flush=True)
        print(k('create', '-f', path), flush=True)
    else:
        shared.apply(name, value)

def preflight(tag, node_name=NODE):
    assert tag.startswith('fs2-bioir-of3-') and node_name == NODE
    shared.preflight(tag, node_name)

def donor(name, run, prepare=False):
    assert name.startswith('fs2-bioir-of3-') and run.startswith('of3-')
    source_path = ROOT.parent.parent / 'openfold/manifests/openfold3-bir-float32-graph-resident-r7-h100.json'
    source = json.loads(source_path.read_text())
    source_cm = next(v for v in source['items'] if v['kind'] == 'ConfigMap')
    pod_spec = copy.deepcopy(next(v for v in source['items'] if v['kind'] == 'Pod')['spec'])
    app = copy.deepcopy(source_cm['data'])
    app['request.py'] = (ROOT / 'request.py').read_text()
    app['cases.json'] = (ROOT.parent.parent / 'openfold/fixtures/cases.json').read_text()
    source_hash = hashlib.sha256(json.dumps(app, sort_keys=True).encode()).hexdigest()
    if prepare:
        source_names = ['supervisor.py', 'process_checkpoint.py', 'serving_checkpoint.py', 'serving_supervisor.py', 'serving_filesystem.py', 'serving_launcher.py', 'sitecustomize.py']
        helper_data = {n: (K8S / 'models/scientific-snapshot' / n).read_text() for n in source_names}
        apply('source', {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'fs2-bioir-of3-snapshot-source', 'namespace': NS, 'labels': LABELS}, 'immutable': True, 'data': helper_data})
        apply('app', {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'fs2-bioir-of3-snapshot-app', 'namespace': NS, 'labels': LABELS}, 'immutable': True, 'data': app})
        save('inventory/source-hashes.json', {'app': {n: hashlib.sha256(t.encode()).hexdigest() for n, t in app.items()}, 'helpers': {n: hashlib.sha256(t.encode()).hexdigest() for n, t in helper_data.items()}, 'source_manifest': str(source_path), 'source_manifest_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest()})
        apply('pvc', {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': PVC, 'namespace': NS, 'labels': LABELS}, 'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'compute-csi-default-sc', 'resources': {'requests': {'storage': '64Gi'}}}})
    preflight(name)
    runtime = pod_spec['containers'][0]
    runtime['name'] = 'boltz2'  # Existing generic supervisor helper's container slot.
    runtime['command'] = ['python', '/snapshot-app/bir_openfold3_server.py']
    for mount in runtime['volumeMounts']:
        if mount['name'] == 'source':
            mount['mountPath'] = '/snapshot-app'
            mount['readOnly'] = True
    next(v for v in pod_spec['volumes'] if v['name'] == 'source')['configMap']['name'] = 'fs2-bioir-of3-snapshot-app'
    sys.path.insert(0, str(ACCEPTANCE / 'h100-fleet/snapshots'))
    import render_serving_probe
    args = argparse.Namespace(container='boltz2', entrypoint_json='[]', asyncio_loop=False, python='python', run=run, fallback='fail', request_uid=None, allow_device_remap=False, mode='donor', tools_image=TOOLS, model_revision='af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4:app:' + source_hash, model_id='openfold3-preview2-bir-0.1.0-fp32-native-rng-graph-resident', source_configmap='fs2-bioir-of3-snapshot-source', pvc=PVC, node=NODE, name=name)
    pod = render_serving_probe.render({'metadata': {'namespace': NS}, 'spec': {'template': {'spec': pod_spec}}}, args)
    pod['metadata']['labels'] = LABELS
    pod['spec']['containers'][0]['command'] = [s.replace('/snapshot-source/supervisor.py', '/snapshot-source/serving_supervisor.py') for s in pod['spec']['containers'][0]['command']]
    pod['spec']['activeDeadlineSeconds'] = 14400
    for service in json.loads(k('-n', NS, 'get', 'services', '-o', 'json'))['items']:
        selector = service['spec'].get('selector')
        assert not selector or not all(LABELS.get(a) == b for a, b in selector.items())
    apply(name, pod)
    save('lifecycle/' + name + '-created.json', {'unix': time.time(), 'run': run, 'model': 'openfold3', 'cuda_graph_requested': True, 'variant': 'normal' if 'normal' in name else 'donor'})

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--name', required=True)
    p.add_argument('--run', required=True)
    p.add_argument('--prepare', action='store_true')
    a = p.parse_args()
    donor(a.name, a.run, a.prepare)
