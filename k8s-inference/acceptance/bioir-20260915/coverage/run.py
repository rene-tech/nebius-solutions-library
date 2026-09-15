#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
import time
from control import K, ROOT, k, save

p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('--namespace',default='fs2-bioir-coverage');p.add_argument('--container');p.add_argument('--batch',action='store_true');p.add_argument('--extended-complexa',action='store_true');a=p.parse_args()
name='fs2-bioir-coverage-'+a.model
deadline=time.time()+1800
while True:
    pod=json.loads(k('-n',a.namespace,'get','pod',name,'-o','json'))
    if pod['status']['phase']=='Running':break
    if pod['status']['phase'] in ['Failed','Succeeded'] or time.time()>deadline:
        save('lifecycle/'+a.model+'-failed.json',pod);raise RuntimeError('Pod did not run')
    time.sleep(5)
save('lifecycle/'+a.model+'-before.json',pod)
container=a.container or pod['spec']['containers'][0]['name']
files=[('batch_client.py',ROOT/'batch_client.py')] if a.batch else [('client.py',ROOT/'client.py'),('cases.json',ROOT/'fixtures/cases.json')]
for remote,local in files:
    expected=hashlib.sha256(local.read_bytes()).hexdigest() if remote!='cases.json' else hashlib.sha256(json.dumps(json.loads(local.read_text())).encode()).hexdigest()
    deadline=time.time()+180
    while True:
        observed=k('-n',a.namespace,'exec',name,'-c',container,'--','python','-c',f'import hashlib;print(hashlib.sha256(open("/eval/{remote}","rb").read()).hexdigest())').strip()
        if observed==expected:break
        if time.time()>deadline:raise RuntimeError('ConfigMap propagation timeout')
        time.sleep(3)
cmd=K+['-n',a.namespace,'exec',name,'-c',container,'--','python','/eval/batch_client.py' if a.batch else '/eval/client.py','--model',a.model]
if a.extended_complexa:cmd.append('--extended-complexa')
(ROOT/'raw').mkdir(exist_ok=True)
for suffix in ['.jsonl','-client-stderr.log','-server.log']:
    old=ROOT/'raw'/(a.model+suffix)
    if old.exists():old.rename(ROOT/'raw'/(a.model+'-previous-'+str(int(time.time()))+suffix))
started=time.time()
with (ROOT/'raw'/f'{a.model}.jsonl').open('w') as out, (ROOT/'raw'/f'{a.model}-client-stderr.log').open('w') as err:
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=err,text=True)
    for line in proc.stdout:
        out.write(line);out.flush()
        try:
            row=json.loads(line)
            print(json.dumps({k:v for k,v in row.items() if k not in ['payload','output','stdout','stderr','validation']}),flush=True)
        except ValueError: print(line[:150],flush=True)
    code=proc.wait()
save('lifecycle/'+a.model+'-after.json',json.loads(k('-n',a.namespace,'get','pod',name,'-o','json')))
save('lifecycle/'+a.model+'-client.json',{'started_unix':started,'completed_unix':time.time(),'returncode':code,'command':cmd})
(ROOT/'raw'/f'{a.model}-server.log').write_text(k('-n',a.namespace,'logs',name,'-c',container))
print('COMPLETE',a.model,'code',code,flush=True)
