#!/usr/bin/env python3
"""Repeat genuine searches using only a new worker's disposable A3M cache."""
import copy,json,subprocess,time
from control import K,ROOT,k,save
name='fs2-bioir-coverage-msa-cold-search';namespace='fs2-bioir-coverage'
pod=json.loads((ROOT/'manifests/msa-search-pdb70.json').read_text())
pod['metadata']['name']=name
pod['spec']['nodeSelector']={'kubernetes.io/hostname':'computeinstance-e00krzha55t0sg3t56'}
assert not any(int(c['resources']['requests'].get('nvidia.com/gpu',0)) for c in pod['spec']['containers'])
cache_mount=next(m for m in pod['spec']['containers'][0]['volumeMounts'] if m['mountPath']=='/cache')
assert 'emptyDir' in next(v for v in pod['spec']['volumes'] if v['name']==cache_mount['name'])
save('manifests/msa-cold-search.json',pod)
print(k('apply','-f',str(ROOT/'manifests/msa-cold-search.json')),flush=True)
try:
    print(k('-n',namespace,'wait','--for=condition=Ready','pod/'+name,'--timeout=600s'),flush=True)
    save('lifecycle/msa-cold-search-before.json',json.loads(k('-n',namespace,'get','pod',name,'-o','json')))
    script='''import hashlib,json,pathlib,sys,time
sys.path.insert(0,'/eval')
from client import http,run,emit
cases=json.load(open('/eval/cases.json'))['msa-search-pdb70']
emit({'type':'ready','health':http('/v1/health/ready'),'cache_policy':'Only exact known A3M files in this task-owned emptyDir are renamed before repeated searches; shared database is unchanged.'})
for rep in range(1,4):
    for case in cases:
        payload=case['payload']
        key=hashlib.sha256(('pdb70_220313\\0'+str(payload['max_msa_sequences'])+'\\0'+payload['sequence']).encode('ascii')).hexdigest()
        path=pathlib.Path('/cache')/(key+'.a3m')
        if path.exists():
            previous=path.with_suffix('.a3m.repetition-'+str(rep));assert not previous.exists()
            digest=hashlib.sha256(path.read_bytes()).hexdigest();path.rename(previous)
            emit({'type':'task_cache_rename','from':str(path),'to':str(previous),'sha256':digest})
        run('msa-search-pdb70',case,rep,'fresh-a3m-cache-miss-warm-os-cache')
emit({'type':'complete','model':'msa-search-pdb70','unix':time.time()})
'''
    with (ROOT/'raw/msa-search-pdb70-repeated-misses.jsonl').open('w') as out,(ROOT/'raw/msa-search-pdb70-repeated-misses-stderr.log').open('w') as err:
        subprocess.run(K+['-n',namespace,'exec','-i',name,'--','python','-'],input=script,text=True,stdout=out,stderr=err,check=True)
    print('repeated search complete',flush=True)
finally:
    save('lifecycle/msa-cold-search-released-pod.json',json.loads(k('-n',namespace,'get','pod',name,'-o','json')))
    (ROOT/'raw/msa-search-pdb70-repeated-misses-server.log').write_text(k('-n',namespace,'logs',name))
    print(k('-n',namespace,'delete','pod',name,'--wait=true','--timeout=60s'),flush=True)
    save('lifecycle/msa-cold-search-released.json',{'unix':time.time(),'pod':name,'gpu_count':0})
