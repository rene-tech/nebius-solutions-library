#!/usr/bin/env python3
"""Hash retained task-owned bundles with a CPU-only, read-only collector."""
import argparse,json,time
from control import ROOT,LABELS,k,save,apply
p=argparse.ArgumentParser();p.add_argument('--model',choices=['boltz2','openfold2'],required=True);a=p.parse_args()
ns='fs2-bioir-boltz2' if a.model=='boltz2' else 'fs2-bioir-snapshot'
claim='fs2-bioir-snapshot-checkpoints' if a.model=='boltz2' else 'fs2-bioir-snapshot-openfold2-checkpoints'
run='boltz2-asyncio-01' if a.model=='boltz2' else 'openfold2-01'
donor='fs2-bioir-snapshot-boltz2-donor-asyncio' if a.model=='boltz2' else 'fs2-bioir-snapshot-openfold2-donor'
source=json.loads((ROOT/'manifests'/f'{donor}.json').read_text());name=f'fs2-bioir-snapshot-{a.model}-bundle-inventory'
script='import hashlib,json,pathlib,time; root=pathlib.Path("/snapshot/'+run+'"); start=time.time(); rows=[]\nfor p in sorted(root.rglob("*")):\n if p.is_file() and not p.is_symlink():\n  h=hashlib.sha256()\n  with p.open("rb") as f:\n   for chunk in iter(lambda:f.read(8*1024*1024),b""):h.update(chunk)\n  rows.append({"path":str(p.relative_to(root)),"bytes":p.stat().st_size,"sha256":h.hexdigest()})\nprint(json.dumps({"run":"'+run+'","files":rows,"bytes":sum(r["bytes"] for r in rows),"hash_seconds":time.time()-start}))'
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':ns,'labels':dict(LABELS,role='bundle-inventory')},'spec':{'restartPolicy':'Never','activeDeadlineSeconds':900,'automountServiceAccountToken':False,'nodeSelector':source['spec']['nodeSelector'],'tolerations':source['spec'].get('tolerations',[]),'containers':[{'name':'collector','image':source['spec']['containers'][0]['image'],'command':['python','-c',script],'resources':{'requests':{'cpu':'1','memory':'512Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'securityContext':{'runAsUser':0,'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']},'seccompProfile':{'type':'RuntimeDefault'}},'volumeMounts':[{'name':'bundle','mountPath':'/snapshot','readOnly':True}]}],'volumes':[{'name':'bundle','persistentVolumeClaim':{'claimName':claim,'readOnly':True}}]}}
apply(name,pod);deadline=time.time()+900
while True:
    value=json.loads(k('-n',ns,'get','pod',name,'-o','json'))
    if value['status']['phase'] in ['Succeeded','Failed']:break
    assert time.time()<deadline;time.sleep(3)
save('lifecycle/'+name+'-observed.json',value)
assert value['status']['phase']=='Succeeded',k('-n',ns,'logs',name)
save('inventory/'+a.model+'-snapshot-bundle.json',json.loads(k('-n',ns,'logs',name)))
assert value['metadata']['labels']['lane']=='snapshot' and value['metadata']['labels']['evaluation']=='fs2-bioir-20260915'
print(k('-n',ns,'delete','pod',name,'--wait=true','--timeout=120s'),flush=True)
save('lifecycle/'+name+'-released.json',{'unix':time.time(),'pod':name,'gpu_count':0})
