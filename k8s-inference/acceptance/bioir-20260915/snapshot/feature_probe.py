#!/usr/bin/env python3
"""Negative HTTP contract probes; run only on owned fallback workers."""
import json,time,urllib.error,urllib.request
paths=['/biology/mit/boltz2/predict','/biology/openfold/openfold2/predict-structure-from-msa-and-template']
rows=[]
for path in paths:
    for label,body in [('missing-required-fields',b'{}'),('malformed-json',b'{')]:
        start=time.perf_counter()
        try:
            with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000'+path,data=body,headers={'Content-Type':'application/json'}),timeout=30) as response:
                status=response.status;content=response.read().decode()
        except urllib.error.HTTPError as exc:status=exc.code;content=exc.read().decode()
        except Exception as exc:status=None;content=type(exc).__name__+': '+str(exc)
        rows.append({'path':path,'case':label,'status':status,'body':content,'seconds':time.perf_counter()-start})
with urllib.request.urlopen('http://127.0.0.1:8000/v1/health/ready',timeout=30) as response:health=json.load(response)
print(json.dumps({'tests':rows,'health_after_errors':health,'cancellation':'No advertised cancellation contract; not qualified by disconnect or process kill.','idempotency':'No request-ID/idempotency contract advertised by these candidates.'}))
