#!/usr/bin/env python3
"""Exact-name cleanup after bundle fingerprints; never delete a namespace."""
import json,time
from control import k,save,preflight
targets=[('fs2-bioir-boltz2','configmap','fs2-bioir-snapshot-app','snapshot'),('fs2-bioir-boltz2','configmap','fs2-bioir-snapshot-source','snapshot'),('fs2-bioir-boltz2','pvc','fs2-bioir-snapshot-checkpoints','snapshot'),('fs2-bioir-snapshot','configmap','fs2-bioir-snapshot-openfold2-app','snapshot'),('fs2-bioir-snapshot','configmap','fs2-bioir-snapshot-source','snapshot'),('fs2-bioir-snapshot','pvc','fs2-bioir-snapshot-openfold2-checkpoints','snapshot'),('fs2-bioir-boltz2','pvc','evaluation-cache','boltz2')]
from control import ROOT
for model in ['boltz2','openfold2']:
    assert (ROOT/'inventory'/f'{model}-snapshot-bundle.json').exists(),model
    assert (ROOT/'lifecycle'/f'fs2-bioir-snapshot-{model}-bundle-inventory-released.json').exists(),model
rows=[]
for namespace,kind,name,lane in targets:
    value=json.loads(k('-n',namespace,'get',kind,name,'-o','json'))
    assert value['metadata']['labels']['evaluation']=='fs2-bioir-20260915' and value['metadata']['labels']['lane']==lane
    pods=json.loads(k('-n',namespace,'get','pods','-o','json'))['items']
    for pod in pods:
        for volume in pod['spec'].get('volumes',[]):
            assert not (kind=='pvc' and volume.get('persistentVolumeClaim',{}).get('claimName')==name),'PVC still mounted'
            assert not (kind=='configmap' and volume.get('configMap',{}).get('name')==name),'ConfigMap still mounted'
    row={'namespace':namespace,'kind':kind,'name':name,'uid':value['metadata']['uid']}
    if kind=='pvc':
        pv=json.loads(k('get','pv',value['spec']['volumeName'],'-o','json'))
        assert pv['spec']['claimRef']['uid']==value['metadata']['uid']
        row.update(pv=pv['metadata']['name'],reclaim_policy=pv['spec']['persistentVolumeReclaimPolicy'])
        save('inventory/cleanup-'+name+'-pv.json',pv)
        assert row['reclaim_policy']=='Delete','Retain policy requires explicit storage handling; do not orphan retained data'
    print(k('-n',namespace,'delete',kind,name,'--dry-run=client'),flush=True)
    print(k('-n',namespace,'delete',kind,name,'--wait=true','--timeout=120s'),flush=True)
    rows.append(row)
    save('lifecycle/final-cleanup.json',{'unix':time.time(),'deleted':rows,'evaluation_cache_authorization':'Explicit parent /root approval after release by Boltz lane; no remaining mounts','namespaces_deleted':False,'note':'Public-weight cache and reproducible CUDA/CRIU bundles removed; raw benchmark/validation evidence, source manifests, image digests and bundle hashes retained.'})
for row in rows:
    if row['kind']!='pvc':continue
    deadline=time.time()+180
    while True:
        value=k('get','pv',row['pv'],'--ignore-not-found','-o','name').strip()
        if not value:row['pv_deleted']=True;break
        if time.time()>deadline:row['pv_deleted']=False;break
        time.sleep(3)
preflight('final-boltz-node','computeinstance-e00y0jttwekyghrznp')
preflight('final-openfold2-node','computeinstance-e00sa78kng1kwhej6q')
save('lifecycle/final-cleanup.json',{'unix':time.time(),'deleted':rows,'namespaces_deleted':False,'production_resources_changed':False,'both_assigned_gpu_nodes_no_allocations_or_processes':True,'note':'Explicit parent approval for Boltz evaluation-cache removal. PVC objects deleted; check each pv_deleted flag for actual reclamation. Any false flag is a separate CSI cleanup follow-up; no finalizer bypass. Raw outputs, provenance and hashes retained.'})
print('CLEANUP REQUESTS COMPLETE; see pv_deleted flags for residual reclamation',flush=True)
