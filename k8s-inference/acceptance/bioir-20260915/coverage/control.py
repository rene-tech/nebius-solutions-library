#!/usr/bin/env python3
"""Freeze live identities and clone only an explicitly named isolated benchmark pod."""
import argparse
import copy
import json
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent
K = ['kubectl', '--kubeconfig', '/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig', '--context', 'k8s-inference-h100']
MODELS = ['diffdock', 'evo2-40b', 'genmol', 'molmim', 'msa-search-pdb70', 'proteinmpnn', 'rfdiffusion', 'proteina-complexa']

def k(*args, **kwargs):
    return subprocess.check_output(K + list(args), text=True, **kwargs)

def save(name, value):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')

def freeze():
    deps = json.loads(k('get', 'deployments', '-A', '-o', 'json'))['items']
    selected = [d for d in deps if any(d['metadata']['name'].startswith(m) for m in MODELS)]
    for d in selected:
        d['metadata'].pop('managedFields', None)
        d['metadata'].pop('annotations', None)
    save('inventory/deployments.json', selected)
    save('inventory/nodes.json', json.loads(k('get', 'nodes', '-o', 'json')))
    pods = json.loads(k('get', 'pods', '-A', '-o', 'json'))['items']
    save('inventory/pod-allocations.json', [{'namespace': p['metadata']['namespace'], 'name': p['metadata']['name'], 'node': p['spec'].get('nodeName'), 'phase': p['status']['phase'], 'containers': [{'name': c['name'], 'image': c['image'], 'resources': c.get('resources')} for c in p['spec']['containers']], 'image_ids': p['status'].get('containerStatuses')} for p in pods])
    for name in ['fs2-serve-control-plane-academic-execution', 'fs2-r927c465c6d-scientific-execution-88c83daa474b']:
        cm = json.loads(k('-n', 'fs2-system', 'get', 'cm', name, '-o', 'json'))
        save('inventory/' + name + '.json', cm.get('data'))
    print('frozen', len(selected), 'deployments')

def clone(model, node, namespace, cache_readonly=False):
    deps = json.loads((ROOT/'inventory/deployments.json').read_text())
    d = next(d for d in deps if d['metadata']['name'] == model or d['metadata']['name'] == model+'-b300-hot-h100-reserved-8x')
    spec = copy.deepcopy(d['spec']['template']['spec'])
    spec['nodeSelector'] = {'kubernetes.io/hostname': node}
    tol={'key':'dedicated','operator':'Equal','value':'fs2-inference','effect':'NoSchedule'}
    if tol not in spec.setdefault('tolerations',[]):spec['tolerations'].append(tol)
    for key in ['affinity', 'topologySpreadConstraints', 'serviceAccount', 'serviceAccountName']:
        spec.pop(key, None)
    spec['restartPolicy'] = 'Never'
    spec['activeDeadlineSeconds'] = 14400
    spec['automountServiceAccountToken'] = False
    spec['initContainers'] = []  # no writes/materialization into retained shared weights
    adjustments=[]
    for c in spec['containers']:
        if cache_readonly:
            for m in c.get('volumeMounts', []):
                if m['name'] == 'model-cache':
                    m['readOnly'] = True
            for e in c.get('env', []):
                if e['name'] in ['CUDA_CACHE_PATH', 'TORCH_EXTENSIONS_DIR', 'TORCHINDUCTOR_CACHE_DIR', 'TRITON_CACHE_DIR', 'HOME']:
                    previous=e.get('value')
                    e['value']='/tmp/fs2-bioir/'+e['name'].lower()
                    adjustments.append({'container':c['name'],'env':e['name'],'old':previous,'new':e['value']})
        c.setdefault('volumeMounts', []).append({'name':'evaluation-client','mountPath':'/eval','readOnly':True})
    spec.setdefault('volumes', []).append({'name':'evaluation-client','configMap':{'name':'fs2-bioir-coverage-client'}})
    name='fs2-bioir-coverage-'+model
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':namespace,'labels':{'evaluation':'fs2-bioir-20260915','lane':'coverage','benchmark-model':model}},'spec':spec}
    services=json.loads(k('-n',namespace,'get','services','-o','json'))['items']
    for service in services:
        selector=service['spec'].get('selector')
        if selector and all(pod['metadata']['labels'].get(key)==val for key,val in selector.items()):
            raise RuntimeError('Existing service would select benchmark: '+service['metadata']['name'])
    save('manifests/'+model+'.json',pod)
    save('manifests/'+model+'-adjustments.json',adjustments)
    print(k('apply','--dry-run=client','-f',str(ROOT/'manifests'/f'{model}.json')).strip())
    print(k('apply','-f',str(ROOT/'manifests'/f'{model}.json')).strip())
    save('lifecycle/'+model+'-created.json',{'unix':time.time(),'pod':name,'namespace':namespace,'source_deployment':d['metadata']['name']})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','clone']);p.add_argument('--model');p.add_argument('--node',default='computeinstance-e00r9tdjfszjs3angk');p.add_argument('--namespace',default='fs2-bioir-coverage');p.add_argument('--cache-readonly',action='store_true');a=p.parse_args()
    if a.action=='freeze': freeze()
    else: clone(a.model,a.node,a.namespace,a.cache_readonly)
