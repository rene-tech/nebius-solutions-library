#!/usr/bin/env python3
"""Render isolated Boltz2 BIR snapshot probes using existing qualified helpers."""
import argparse,copy,hashlib,json,pathlib,subprocess,sys,time
ROOT=pathlib.Path(__file__).resolve().parent;ACCEPTANCE=ROOT.parents[1];K8S=ROOT.parents[2]
K=['kubectl','--kubeconfig','/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig','--context','k8s-inference-h100']
NS='fs2-bioir-boltz2';NODE='computeinstance-e00y0jttwekyghrznp';PVC='fs2-bioir-snapshot-checkpoints'
TOOLS='cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4'
LABELS={'evaluation':'fs2-bioir-20260915','lane':'snapshot','benchmark-model':'boltz2-bir'}
def k(*args,**kwargs):return subprocess.check_output(K+list(args),text=True,**kwargs)
def save(name,value):
    path=ROOT/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2)+'\n')
def apply(name,value):
    save('manifests/'+name+'.json',value);print(k('apply','--dry-run=client','-f',str(ROOT/'manifests'/f'{name}.json')),flush=True);print(k('apply','-f',str(ROOT/'manifests'/f'{name}.json')),flush=True)
def prepare():
    source=K8S/'models/scientific-snapshot'
    names=['supervisor.py','process_checkpoint.py','serving_checkpoint.py','serving_supervisor.py','serving_filesystem.py','serving_launcher.py','sitecustomize.py']
    data={name:(source/name).read_text() for name in names}
    apply('source',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fs2-bioir-snapshot-source','namespace':NS,'labels':LABELS},'immutable':True,'data':data})
    boltz=ROOT.parent/'boltz2';names=['server.py','bir_worker.py','gpu_preflight.py','benchmark_http.py']
    data={name:(boltz/name).read_text() for name in names};data['request.py']=(ROOT/'request.py').read_text()
    apply('app',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fs2-bioir-snapshot-app','namespace':NS,'labels':LABELS},'immutable':True,'data':data})
    save('inventory/source-hashes.json',{name:hashlib.sha256(text.encode()).hexdigest() for name,text in data.items()})
    apply('pvc',{'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':PVC,'namespace':NS,'labels':LABELS},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'compute-csi-default-sc','resources':{'requests':{'storage':'128Gi'}}}})
def preflight(tag,node_name=NODE):
    pods=json.loads(k('get','pods','-A','-o','json'))['items'];node=json.loads(k('get','node',node_name,'-o','json'))
    allocated=[p for p in pods if p['spec'].get('nodeName')==node_name and p['status']['phase'] in ['Running','Pending'] and any(int(c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu',0)) for c in p['spec']['containers'])]
    save('inventory/'+tag+'-preflight.json',{'unix':time.time(),'node':node,'gpu_allocated_pods':[{'name':p['metadata']['name'],'namespace':p['metadata']['namespace']} for p in allocated],'pending_gpu_customer_pods':[{'namespace':p['metadata']['namespace'],'name':p['metadata']['name']} for p in pods if p['status']['phase']=='Pending' and not p['metadata'].get('labels',{}).get('evaluation') and any(int(c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu',0)) for c in p['spec']['containers'])]})
    assert not allocated,'Assigned node has an allocated GPU workload'
    assert any(c['type']=='Ready' and c['status']=='True' for c in node['status']['conditions'])
    detector=next(p['metadata']['name'] for p in pods if p['spec'].get('nodeName')==node_name and p['metadata']['name'].startswith('nebius-node-problem-detector-gpu-'))
    observed=k('-n','kube-system','exec',detector,'--','/usr/bin/nvidia-smi','--query-compute-apps=pid,process_name,used_gpu_memory','--format=csv,noheader').strip()
    save('inventory/'+tag+'-gpu-processes.json',{'unix':time.time(),'stdout':observed});assert not observed
def donor(name,run,loop):
    preflight(name)
    sys.path.insert(0,str(ACCEPTANCE/'h100-fleet/snapshots'))
    import render_serving_probe
    original=json.loads((ROOT.parent/'boltz2/bir-h100.json').read_text());spec=original['spec'];runtime=spec['containers'][0]
    runtime['command']=['python','-m','uvicorn','bir_worker:app','--app-dir','/snapshot-app','--host','0.0.0.0','--port','8000']
    if loop=='asyncio':runtime['command']+=['--loop','asyncio']
    runtime['resources']['limits']['cpu']='16'
    runtime['volumeMounts'][0]['readOnly']=True
    runtime['volumeMounts'].extend([{'name':'app','mountPath':'/snapshot-app','readOnly':True},{'name':'snapshot-checkpoints','mountPath':'/runtime-cache'}])
    spec['volumes'].append({'name':'app','configMap':{'name':'fs2-bioir-snapshot-app'}})
    for item in runtime['env']:
        if item['name']=='BIOIR_CACHE':item['value']='/runtime-cache/bioir-cache'
    runtime['env'] += [{'name':'HF_HUB_OFFLINE','value':'1'},{'name':'TRANSFORMERS_OFFLINE','value':'1'}]
    source={'metadata':{'namespace':NS},'spec':{'template':{'spec':spec}}}
    args=argparse.Namespace(container=runtime['name'],entrypoint_json='[]',asyncio_loop=False,python='python',run=run,fallback='fail',request_uid=None,allow_device_remap=False,mode='donor',tools_image=TOOLS,model_revision='6fdef46d763fee7fbb83ca5501ccceff43b85607',model_id='boltz2-bir-public-0.1.0',source_configmap='fs2-bioir-snapshot-source',pvc=PVC,node=NODE,name=name)
    pod=render_serving_probe.render(source,args);pod['metadata']['labels']=dict(LABELS,loop=loop)
    runtime=pod['spec']['containers'][0];runtime['command']=[s.replace('/snapshot-source/supervisor.py','/snapshot-source/serving_supervisor.py') for s in runtime['command']]
    pod['spec']['activeDeadlineSeconds']=14400
    initializer=pod['spec']['initContainers'][0]
    initializer['volumeMounts'].append({'name':'cache','mountPath':'/models','readOnly':True})
    initializer['command'][2]+=' && cp -a /models/bioir-cache "$1/runtime-cache/bioir-cache"'
    # The source has no production labels; assert no existing service selects this probe.
    for service in json.loads(k('-n',NS,'get','services','-o','json'))['items']:
        selector=service['spec'].get('selector');assert not selector or not all(pod['metadata']['labels'].get(a)==b for a,b in selector.items())
    apply(name,pod);save('lifecycle/'+name+'-created.json',{'unix':time.time(),'run':run,'loop':loop})
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','donor']);p.add_argument('--name');p.add_argument('--run');p.add_argument('--loop',default='auto',choices=['auto','asyncio']);a=p.parse_args()
    if a.action=='prepare':prepare()
    else:donor(a.name,a.run,a.loop)
