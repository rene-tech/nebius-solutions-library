#!/usr/bin/env python3
"""Record lifecycle, capture and requests only for explicitly owned probe pods."""
import argparse,copy,json,subprocess,time
from control import K,NS,ROOT,LABELS,PVC,k,save,apply,preflight
def pod(name):
    value=json.loads(k('-n',NS,'get','pod',name,'-o','json'))
    assert all(value['metadata']['labels'].get(key)==LABELS[key] for key in ['evaluation','lane'])
    return value
def directory(value):
    command=value['spec']['containers'][0]['command'];return command[command.index('--directory')+1]
def collect(name):
    value=pod(name);save('lifecycle/'+name+'-observed.json',value)
    base=directory(value)
    for suffix,path in [('worker.log',base+'/worker.log'),('supervisor.log',None),('dump.log',base+'/images/dump.log'),('restore.log','/tmp/fs2-checkpoint-work/restore.log')]:
        try:text=k('-n',NS,'logs',name,'-c','boltz2') if path is None else k('-n',NS,'exec',name,'-c','boltz2','--','cat',path,stderr=subprocess.DEVNULL)
        except Exception:continue
        target=ROOT/'raw'/name/suffix;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
    try:
        target=ROOT/'raw'/name/'requests.tgz'
        with target.open('wb') as stream:subprocess.run(K+['-n',NS,'exec',name,'-c','boltz2','--','tar','-czf','-','-C','/tmp','evaluation-results'],stdout=stream,stderr=subprocess.DEVNULL,check=True)
    except Exception:pass
def ready(name):
    start=time.time();deadline=start+1800;script='import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/v1/health/ready",timeout=2))))'
    while time.time()<deadline:
        value=pod(name)
        if value['status']['phase']=='Failed':collect(name);raise RuntimeError('Probe failed')
        try:
            health=json.loads(k('-n',NS,'exec',name,'-c','boltz2','--','python','-c',script,stderr=subprocess.DEVNULL));break
        except Exception:time.sleep(3)
    else:collect(name);raise RuntimeError('Readiness timeout')
    save('lifecycle/'+name+'-ready.json',{'unix':time.time(),'observed_wait_seconds':time.time()-start,'health':health,'pod':value});print('READY',name,json.dumps(health),flush=True)
def request(name,cases,label,samples):
    pod(name);path=ROOT/'raw'/name;path.mkdir(parents=True,exist_ok=True)
    with (path/(label+'.jsonl')).open('w') as out,(path/(label+'-stderr.log')).open('w') as err:
        result=subprocess.run(K+['-n',NS,'exec',name,'-c','boltz2','--','python','/snapshot-app/request.py','--cases',cases,'--out','/tmp/evaluation-results/'+label,'--samples',str(samples)],stdout=out,stderr=err)
    rows=[json.loads(line) for line in (path/(label+'.jsonl')).read_text().splitlines()]
    print(json.dumps({'pod':name,'label':label,'returncode':result.returncode,'attempts':[{key:r.get(key) for key in ['fixture','valid','validated_seconds','error']} for r in rows]}),flush=True);collect(name)
def capture(name):
    value=pod(name);base=directory(value);path=ROOT/'raw'/name;path.mkdir(parents=True,exist_ok=True)
    script='import pathlib,subprocess; p=pathlib.Path('+repr(base)+'); pid=int((p/"live-worker-pid").read_text()); assert pid>1; raise SystemExit(subprocess.call(["python","/snapshot-source/serving_checkpoint.py","capture","--pid",str(pid),"--directory",str(p/"images")]))'
    start=time.time()
    with (path/'capture-stdout.json').open('w') as out,(path/'capture-stderr.log').open('w') as err:result=subprocess.run(K+['-n',NS,'exec',name,'-c','boltz2','--','python','-c',script],stdout=out,stderr=err)
    save('lifecycle/'+name+'-capture.json',{'started_unix':start,'completed_unix':time.time(),'returncode':result.returncode});collect(name);print('CAPTURE',name,result.returncode,flush=True)
def release(name):
    if name.endswith('-fallback'):
        pod(name)
        result=k('-n',NS,'exec',name,'-c','boltz2','--','python','-c',(ROOT/'feature_probe.py').read_text())
        save('raw/'+name+'/features.json',json.loads(result))
    collect(name);save('lifecycle/'+name+'-before-delete.json',pod(name));print(k('-n',NS,'delete','pod',name,'--wait=true','--timeout=180s'),flush=True);save('lifecycle/'+name+'-released.json',{'unix':time.time(),'pod':name})
def restore(source,name,fallback=False):
    source=pod(source) if not (ROOT/'lifecycle'/(source+'-before-delete.json')).exists() else json.loads((ROOT/'lifecycle'/(source+'-before-delete.json')).read_text())
    preflight(name,source['spec']['nodeSelector']['kubernetes.io/hostname'])
    value={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':NS,'labels':source['metadata']['labels']},'spec':copy.deepcopy(source['spec'])}
    spec=value['spec'];spec.pop('nodeName',None);runtime=spec['containers'][0];command=runtime['command'];base=directory(value);relative=base.removeprefix('/checkpoints/')
    command[command.index('donor'):command.index('donor')+1]=['--source-directory','/snapshot-bundle','restore']
    if fallback:
        command[command.index('--fallback')+1]='normal-load'
        next(v for v in runtime['env'] if v['name']=='FS2_MODEL_REVISION')['value']='intentional-incompatible-revision-fallback-test'
    volume=next(v for v in spec['volumes'] if v['name']=='snapshot-checkpoints');claim=volume['persistentVolumeClaim']['claimName'];volume.clear();volume.update(name='snapshot-checkpoints',emptyDir={})
    spec['volumes'].append({'name':'snapshot-bundle','persistentVolumeClaim':{'claimName':claim,'readOnly':True}})
    bundle={'name':'snapshot-bundle','mountPath':'/snapshot-bundle','subPath':relative,'readOnly':True}
    runtime['volumeMounts'] += [bundle,{'name':'snapshot-bundle','mountPath':base+'/images','subPath':relative+'/images','readOnly':True}]
    init=spec['initContainers'][0];init['volumeMounts'].append(bundle)
    init['command'][2]=init['command'][2].replace(' && cp -a /models/bioir-cache "$1/runtime-cache/bioir-cache"','').replace('"$1/cache" ','')
    init['command'][2]+=' && for part in runtime-cache tmp; do cp -a /snapshot-bundle/$part/. "$1/$part/"; done'
    apply(name,value);save('lifecycle/'+name+'-created.json',{'unix':time.time(),'source_pod':source['metadata']['name'],'variant':'fallback' if fallback else 'restore'})
p=argparse.ArgumentParser();p.add_argument('action',choices=['ready','request','capture','release','restore','collect']);p.add_argument('name');p.add_argument('--source');p.add_argument('--cases',default='T1031,T1038');p.add_argument('--label',default='measured');p.add_argument('--samples',type=int,default=1);p.add_argument('--fallback',action='store_true');p.add_argument('--namespace',default=NS);a=p.parse_args();NS=a.namespace
if a.action=='request':request(a.name,a.cases,a.label,a.samples)
elif a.action=='restore':restore(a.source,a.name,a.fallback)
else:globals()[a.action](a.name)
