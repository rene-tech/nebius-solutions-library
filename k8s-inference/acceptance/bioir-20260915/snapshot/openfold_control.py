#!/usr/bin/env python3
"""Own L40S clone of the validated BIR OpenFold2 mixed-precision worker."""
import argparse,copy,json,sys,time
from control import ROOT,K8S,ACCEPTANCE,TOOLS,LABELS,apply,save,preflight
NS='fs2-bioir-snapshot';NODE='computeinstance-e00sa78kng1kwhej6q';PVC='fs2-bioir-snapshot-openfold2-checkpoints'
p=argparse.ArgumentParser();p.add_argument('--name',required=True);p.add_argument('--run',required=True);p.add_argument('--prepare',action='store_true');a=p.parse_args()
source=json.loads((ROOT.parent/'openfold/manifests/openfold2-bir-mixed-r2-l40s.json').read_text());spec=copy.deepcopy(next(v for v in source['items'] if v['kind']=='Pod')['spec'])
labels=dict(LABELS,**{'benchmark-model':'openfold2-bir'})
if a.prepare:
    apply('of2-namespace',{'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{'evaluation':LABELS['evaluation'],'lane':'snapshot'}}})
    data=json.loads((ROOT/'manifests/source.json').read_text());data['metadata']['namespace']=NS;apply('of2-source',data)
    app={name:(ROOT.parent/'openfold'/name).read_text() for name in ['bir_openfold2_server.py','benchmark_http.py']};app['request.py']=(ROOT/'openfold_request.py').read_text()
    cases=json.loads((ROOT.parent/'openfold/fixtures/cases.json').read_text())
    app['cases.json']=json.dumps([{'id':c['id'],'feature':c['feature'],'chains':[{'id':v['id'],'sequence':v['sequence']} for v in c['chains']]} for c in cases if c['feature']=='monomer'])
    apply('of2-app',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fs2-bioir-snapshot-openfold2-app','namespace':NS,'labels':labels},'immutable':True,'data':app})
    apply('of2-pvc',{'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':PVC,'namespace':NS,'labels':labels},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'compute-csi-default-sc','resources':{'requests':{'storage':'64Gi'}}}})
preflight(a.name,NODE)
runtime=spec['containers'][0];runtime['name']='boltz2' # generic harness container slot; benchmark-model label remains openfold2-bir
runtime['command']=['python','/snapshot-app/bir_openfold2_server.py'];runtime['resources']['limits']['cpu']='16'
runtime['volumeMounts']=[v for v in runtime['volumeMounts'] if v['name']!='source']+[{'name':'source','mountPath':'/snapshot-app','readOnly':True}]
for volume in spec['volumes']:
    if volume['name']=='source':volume['configMap']['name']='fs2-bioir-snapshot-openfold2-app'
runtime['env'] += [{'name':'OPENFOLD_CACHE','value':'/tmp/openfold-cache'},{'name':'TORCHINDUCTOR_CACHE_DIR','value':'/tmp/torchinductor-cache'},{'name':'USER','value':'bioir-evaluation'}]
sys.path.insert(0,str(ACCEPTANCE/'h100-fleet/snapshots'));import render_serving_probe
args=argparse.Namespace(container='boltz2',entrypoint_json='[]',asyncio_loop=False,python='python',run=a.run,fallback='fail',request_uid=None,allow_device_remap=False,mode='donor',tools_image=TOOLS,model_revision='finetuning_no_templ_ptm_1',model_id='openfold2-bir-public-0.1.0-mixed',source_configmap='fs2-bioir-snapshot-source',pvc=PVC,node=NODE,name=a.name)
pod=render_serving_probe.render({'metadata':{'namespace':NS},'spec':{'template':{'spec':spec}}},args);pod['metadata']['labels']=labels
runtime=pod['spec']['containers'][0];runtime['command']=[s.replace('/snapshot-source/supervisor.py','/snapshot-source/serving_supervisor.py') for s in runtime['command']]
pod['spec']['activeDeadlineSeconds']=14400
apply(a.name,pod);save('lifecycle/'+a.name+'-created.json',{'unix':time.time(),'run':a.run,'model':'openfold2','graphs':False})
