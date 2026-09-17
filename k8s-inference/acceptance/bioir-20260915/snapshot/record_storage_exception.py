#!/usr/bin/env python3
"""Retain read-only CSI reclamation evidence without bypassing protection."""
import json,time
from control import k,save
name='pvc-cf75a701-6ede-4d91-935e-af6116917554'
text=k('get','pv',name,'--ignore-not-found','-o','json')
pv=json.loads(text) if text.strip() else None
events=json.loads(k('get','events','-A','--field-selector','involvedObject.name='+name,'-o','json'))['items']
save('lifecycle/storage-reclamation-exception.json',{'unix':time.time(),'pv':pv,'events':[{'reason':event.get('reason'),'message':event.get('message'),'firstTimestamp':event.get('firstTimestamp'),'lastTimestamp':event.get('lastTimestamp'),'count':event.get('count'),'reportingController':event.get('reportingController')} for event in events],'former_pvc':'fs2-bioir-boltz2/evaluation-cache','authorization':'Parent directed preservation as a separate storage follow-up; no scope expansion','action':'PVC deletion requested and completed. No finalizer removal, force deletion, controller modification or backing-directory removal.','status':'pending-controller-reclamation' if pv else 'reclaimed-after-controller-retry'})
print('STORAGE', 'PENDING' if pv else 'RECLAIMED',flush=True)
