#!/usr/bin/env python3
"""Delete exact owned client ConfigMaps only after every benchmark pod is gone."""
import json,time
from control import k,save
pods=json.loads(k('get','pods','-A','-l','evaluation=fs2-bioir-20260915,lane=coverage','-o','json'))['items'];assert not pods
targets=[('fs2-bioir-coverage','fs2-bioir-coverage-batch'),('fs2-bioir-coverage','fs2-bioir-coverage-client'),('fs2-models','fs2-bioir-coverage-client')];rows=[]
for namespace,name in targets:
    value=json.loads(k('-n',namespace,'get','configmap',name,'-o','json'))
    assert value['metadata']['labels']['lane']=='coverage' and value['metadata']['labels']['evaluation']=='fs2-bioir-20260915'
    print(k('-n',namespace,'delete','configmap',name,'--dry-run=client'))
    print(k('-n',namespace,'delete','configmap',name))
    rows.append({'namespace':namespace,'kind':'ConfigMap','name':name,'reproducible_from_manifests':True})
save('lifecycle/final-cleanup.json',{'unix':time.time(),'deleted':rows,'remaining_benchmark_pods':0,'production_resources_changed':False,'namespace':'Empty evaluation namespace retained; no PVC was created by coverage.'})
