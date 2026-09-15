#!/usr/bin/env python3
"""Isolated exact-image workers for models served as scientific batch jobs."""
import argparse,json,subprocess,time
from control import K,ROOT,k,save
p=argparse.ArgumentParser();p.add_argument('model',choices=['rfdiffusion','proteina-complexa']);p.add_argument('--node',required=True);a=p.parse_args()
mapping=json.loads(json.loads((ROOT/'inventory/fs2-r927c465c6d-scientific-execution-88c83daa474b.json').read_text())['execution-map.json'])
model=next(m for m in mapping['models'] if m['model_id']==a.model)
stage=model['stages'][0]
volumes=[{'name':'work','emptyDir':{'sizeLimit':'32Gi'}},{'name':'eval','configMap':{'name':'fs2-bioir-coverage-batch'}}]
mounts=[{'name':'work','mountPath':'/work'},{'name':'eval','mountPath':'/eval','readOnly':True}]
for m in stage['mounts']:
    if m['kind']=='reference':
        volumes.append({'name':m['name'],'hostPath':{'path':m['host_path'],'type':'Directory'}})
        mounts.append({'name':m['name'],'mountPath':m['mount_path'],'subPath':m['sub_path'],'readOnly':True})
resources={k:{key.replace('ephemeral_storage','ephemeral-storage'):v for key,v in value.items()} for k,value in stage['resources'].items()}
for resource in resources.values():resource['nvidia.com/gpu']='1'
pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':'fs2-bioir-coverage-'+a.model,'namespace':'fs2-bioir-coverage','labels':{'evaluation':'fs2-bioir-20260915','lane':'coverage','benchmark-model':a.model}},'spec':{'restartPolicy':'Never','activeDeadlineSeconds':14400,'automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':a.node},'tolerations':[{'key':'dedicated','operator':'Equal','value':'fs2-inference','effect':'NoSchedule'}],'securityContext':{'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'supplementalGroups':[1000],'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':'runtime','image':stage['image'],'imagePullPolicy':'IfNotPresent','command':['python','-c','import time; time.sleep(14400)'],'resources':resources,'volumeMounts':mounts,'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}}],'volumes':volumes}}
cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fs2-bioir-coverage-batch','namespace':'fs2-bioir-coverage','labels':{'evaluation':'fs2-bioir-20260915','lane':'coverage'}},'data':{'batch_client.py':(ROOT/'batch_client.py').read_text()}}
save('manifests/batch-client.json',cm);save('manifests/'+a.model+'.json',pod)
for file in ['batch-client.json',a.model+'.json']:
    print(k('apply','--dry-run=client','-f',str(ROOT/'manifests'/file)))
    print(k('apply','-f',str(ROOT/'manifests'/file)))
save('lifecycle/'+a.model+'-created.json',{'unix':time.time(),'pod':pod['metadata']['name'],'namespace':'fs2-bioir-coverage','current_image':stage['image'],'runtime_entrypoint':'executed per request by batch_client.py; idle parent owns allocation between requests'})
