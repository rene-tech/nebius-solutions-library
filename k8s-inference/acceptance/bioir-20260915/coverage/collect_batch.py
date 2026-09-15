#!/usr/bin/env python3
"""Retain outputs/logs from one owned scientific-batch worker before release."""
import argparse,hashlib,json,subprocess,time
from control import K,ROOT,k,save
p=argparse.ArgumentParser();p.add_argument('model');a=p.parse_args()
name='fs2-bioir-coverage-'+a.model
pod=json.loads(k('-n','fs2-bioir-coverage','get','pod',name,'-o','json'))
assert pod['metadata']['labels']['evaluation']=='fs2-bioir-20260915'
assert pod['metadata']['labels']['lane']=='coverage'
target=ROOT/'artifacts'/(a.model+'-workspace.tgz');target.parent.mkdir(exist_ok=True)
with target.open('wb') as stream:
    subprocess.run(K+['-n','fs2-bioir-coverage','exec',name,'--','tar','-czf','-','-C','/work','.'],stdout=stream,check=True)
save('artifacts/'+a.model+'-receipt.json',{'unix':time.time(),'path':str(target.relative_to(ROOT)),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size,'symlinks_dereferenced':False})
print(target,target.stat().st_size)
