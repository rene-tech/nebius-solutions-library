#!/usr/bin/env python3
"""Read-only before/after readiness audit of the shared Evo2 host's original pods."""
import json,time
from control import ROOT,k,save
before=json.loads((ROOT/'inventory/pod-allocations.json').read_text())
names={(p['namespace'],p['name']):p for p in before if p['node']=='computeinstance-e00m0hsph76ajt9sdb' and p['phase']=='Running' and any(int(c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu',0)) for c in p['containers'])}
pods=json.loads(k('get','pods','-A','-o','json'))['items'];observed={(p['metadata']['namespace'],p['metadata']['name']):p for p in pods};rows=[]
for key,previous in names.items():
    current=observed.get(key)
    rows.append({'namespace':key[0],'pod':key[1],'before':[{'name':s['name'],'ready':s['ready'],'restarts':s['restartCount'],'image_id':s['imageID']} for s in previous['image_ids']],'after':None if current is None else [{'name':s['name'],'ready':s['ready'],'restarts':s['restartCount'],'image_id':s['imageID']} for s in current['status'].get('containerStatuses',[])],'after_phase':current['status']['phase'] if current else None})
save('inventory/evo2-shared-host-after-health.json',{'unix':time.time(),'node':'computeinstance-e00m0hsph76ajt9sdb','original_gpu_pods':rows,'all_original_ready':all(r['after'] and all(c['ready'] for c in r['after']) for r in rows),'note':'Kubernetes readiness/restart/image audit only, not an application latency or SLO guarantee. No customer inference requests sent.'})
print(json.dumps({'original_pods':len(rows),'all_ready':all(r['after'] and all(c['ready'] for c in r['after']) for r in rows)}))
