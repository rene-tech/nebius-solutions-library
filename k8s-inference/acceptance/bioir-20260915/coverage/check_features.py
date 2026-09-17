#!/usr/bin/env python3
import argparse,json,subprocess
from control import K,ROOT,save
p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('--namespace',default='fs2-models');a=p.parse_args()
code='''
import json,time,uuid,urllib.request,urllib.error
model=MODEL
case=json.load(open('/eval/cases.json'))[model][0]
payload=dict(case['payload']);payload['unrecognized_parameter']=True
rid='bioir-feature-'+uuid.uuid4().hex
out=[]
requests=[('reject-unknown-parameter',payload,rid)]
if model=='evo2-40b':
    valid=dict(case['payload']);valid['num_tokens']=2
    requests += [('valid-idempotency-seed',valid,rid+'valid'),('reject-replayed-request-id',valid,rid+'valid')]
for label,data,request_id in requests:
    start=time.time();status=None
    request=urllib.request.Request('http://127.0.0.1:8000'+case['endpoint'],data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Request-ID':request_id})
    try:
        with urllib.request.urlopen(request,timeout=180) as response:status=response.status;body=json.load(response)
    except urllib.error.HTTPError as error:status=error.code;body=json.loads(error.read())
    out.append({'case':label,'status':status,'seconds':time.time()-start,'body':body})
print(json.dumps(out))
'''.replace('MODEL',repr(a.model))
container='model' if a.model=='evo2-40b' else 'genmol'
out=subprocess.check_output(K+['-n',a.namespace,'exec','fs2-bioir-coverage-'+a.model,'-c',container,'--','python','-c',code],text=True)
save('raw/'+a.model+'-features.json',json.loads(out));print(out)
