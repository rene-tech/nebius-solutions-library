#!/usr/bin/env python3
import argparse,json,time
from control import k,save
p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('--namespace',default='fs2-bioir-coverage');a=p.parse_args()
name='fs2-bioir-coverage-'+a.model
pod=json.loads(k('-n',a.namespace,'get','pod',name,'-o','json'))
assert pod['metadata']['labels']['evaluation']=='fs2-bioir-20260915' and pod['metadata']['labels']['lane']=='coverage'
save('lifecycle/'+a.model+'-released-pod.json',pod)
try:
    identity=k('-n',a.namespace,'exec',name,'-c',pod['spec']['containers'][0]['name'],'--','python','-c','import json,pathlib; p=pathlib.Path("/opt/fs2/model/build-inputs.json");print(p.read_text() if p.exists() else "null")')
    save('inventory/'+a.model+'-build-inputs.json',json.loads(identity))
except Exception:pass
save('lifecycle/'+a.model+'-events.json',json.loads(k('-n',a.namespace,'get','events','--field-selector','involvedObject.name='+name,'-o','json')))
print(k('-n',a.namespace,'delete','pod',name,'--wait=true','--timeout=60s'))
save('lifecycle/'+a.model+'-released.json',{'unix':time.time(),'pod':name,'namespace':a.namespace})
