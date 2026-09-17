#!/usr/bin/env python3
"""Fingerprint then remove only owned snapshot temporary objects and storage."""
import json
import time
from control import ROOT, NS, NODE, PVC, LABELS, apply, k, save, preflight

name = 'fs2-bioir-of3-bundle-inventory'
preflight(name)
image = json.loads((ROOT / 'manifests/fs2-bioir-of3-donor.json').read_text())['spec']['containers'][0]['image']
pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NS, 'labels': dict(LABELS, role='bundle-inventory')}, 'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'nodeSelector': {'kubernetes.io/hostname': NODE}, 'tolerations': [{'key': 'dedicated', 'operator': 'Equal', 'value': 'fs2-inference', 'effect': 'NoSchedule'}], 'securityContext': {'runAsUser': 0, 'runAsGroup': 0}, 'containers': [{'name': 'inventory', 'image': image, 'command': ['python', '-c', (ROOT / 'bundle_inventory.py').read_text()], 'resources': {'requests': {'cpu': '1', 'memory': '256Mi'}, 'limits': {'cpu': '2', 'memory': '2Gi'}}, 'volumeMounts': [{'name': 'bundle', 'mountPath': '/bundle', 'readOnly': True}]}], 'volumes': [{'name': 'bundle', 'persistentVolumeClaim': {'claimName': PVC, 'readOnly': True}}]}}
apply(name, pod)
deadline = time.time() + 1200
while True:
    observed = json.loads(k('-n', NS, 'get', 'pod', name, '-o', 'json'))
    if observed['status']['phase'] in ('Failed', 'Succeeded'):
        break
    assert time.time() < deadline, 'Inventory timeout; preserve pod/PVC for inspection'
    time.sleep(3)
save('inventory/bundle-inventory-pod.json', observed)
assert observed['status']['phase'] == 'Succeeded'
save('inventory/bundle.json', json.loads(k('-n', NS, 'logs', name)))
print(k('-n', NS, 'delete', 'pod', name, '--wait=true', '--timeout=180s'), flush=True)
deleted = []
for kind, target in [('configmap', 'fs2-bioir-of3-snapshot-app'), ('configmap', 'fs2-bioir-of3-snapshot-source'), ('pvc', PVC)]:
    value = json.loads(k('-n', NS, 'get', kind, target, '-o', 'json'))
    assert all(value['metadata']['labels'].get(a) == b for a, b in LABELS.items())
    for p in json.loads(k('-n', NS, 'get', 'pods', '-o', 'json'))['items']:
        for volume in p['spec'].get('volumes', []):
            assert not (kind == 'pvc' and volume.get('persistentVolumeClaim', {}).get('claimName') == target)
            assert not (kind == 'configmap' and volume.get('configMap', {}).get('name') == target)
    receipt = {'kind': kind, 'name': target, 'uid': value['metadata']['uid']}
    if kind == 'pvc':
        pv = json.loads(k('get', 'pv', value['spec']['volumeName'], '-o', 'json'))
        assert pv['spec']['claimRef']['uid'] == value['metadata']['uid'] and pv['spec']['persistentVolumeReclaimPolicy'] == 'Delete'
        save('inventory/deleted-pv.json', pv)
        receipt['pv'] = pv['metadata']['name']
    print(k('-n', NS, 'delete', kind, target, '--wait=true', '--timeout=180s'), flush=True)
    deleted.append(receipt)
    save('lifecycle/final-cleanup.json', {'unix': time.time(), 'deleted': deleted, 'namespace_deleted': False, 'production_changed': False})
for receipt in deleted:
    if 'pv' not in receipt:
        continue
    deadline = time.time() + 180
    while time.time() < deadline and k('get', 'pv', receipt['pv'], '--ignore-not-found', '-o', 'name').strip():
        time.sleep(3)
    receipt['pv_deleted'] = not k('get', 'pv', receipt['pv'], '--ignore-not-found', '-o', 'name').strip()
preflight('fs2-bioir-of3-final-free')
save('lifecycle/final-cleanup.json', {'unix': time.time(), 'deleted': deleted, 'namespace_deleted': False, 'production_changed': False, 'gpu_no_allocations_or_processes': True, 'note': 'Reproducible temporary CUDA/CRIU bundles removed after fingerprinting. Raw outputs, manifests, identities, logs and hashes retained; snapshot pages require recapture.'})
