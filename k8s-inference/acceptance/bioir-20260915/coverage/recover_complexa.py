#!/usr/bin/env python3
"""Repeat one transport-truncated trial; preserve the original partial stream."""
import hashlib,json,subprocess,time
from control import K,ROOT,k,save
name='fs2-bioir-coverage-proteina-complexa';namespace='fs2-bioir-coverage'
pod=json.loads(k('-n',namespace,'get','pod',name,'-o','json'))
assert pod['metadata']['labels']['lane']=='coverage' and pod['metadata']['labels']['evaluation']=='fs2-bioir-20260915'
cm=json.loads((ROOT/'manifests/batch-client.json').read_text());cm['data']['batch_client.py']=(ROOT/'batch_client.py').read_text();save('manifests/batch-client-recovery.json',cm)
print(k('apply','--dry-run=client','-f',str(ROOT/'manifests/batch-client-recovery.json')))
print(k('apply','-f',str(ROOT/'manifests/batch-client-recovery.json')))
expected=hashlib.sha256((ROOT/'batch_client.py').read_bytes()).hexdigest()
deadline=time.time()+180
while True:
    observed=k('-n',namespace,'exec',name,'--','python','-c','import hashlib;print(hashlib.sha256(open("/eval/batch_client.py","rb").read()).hexdigest())').strip()
    if observed==expected:break
    assert time.time()<deadline;time.sleep(3)
save('raw/proteina-complexa-transport-failure.json',{'event':'kubectl exec stream reset during final emitted row','original_evidence':'raw/proteina-complexa.jsonl','stderr':'raw/proteina-complexa-client-stderr.log','case':'38_TNFalpha','repetition':3,'seed':45,'timing_recoverable':False,'disposition':'Keep original artifacts and truncated output; repeat only this exact case with durable per-trial JSON before emission.'})
start=time.time()
with (ROOT/'raw/proteina-complexa-recovery.jsonl').open('w') as out,(ROOT/'raw/proteina-complexa-recovery-client-stderr.log').open('w') as err:
    result=subprocess.run(K+['-n',namespace,'exec',name,'--','python','/eval/batch_client.py','--model','proteina-complexa','--extended-complexa','--only-case','38_TNFalpha','--only-repetition','3'],stdout=out,stderr=err)
save('lifecycle/proteina-complexa-recovery-client.json',{'started_unix':start,'completed_unix':time.time(),'returncode':result.returncode})
print('RECOVERY FINISHED',result.returncode,flush=True)
