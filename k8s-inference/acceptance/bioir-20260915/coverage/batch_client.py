#!/usr/bin/env python3
"""Run current scientific batch entrypoints and retain validated artifacts."""
import argparse,hashlib,json,os,pathlib,subprocess,time,uuid

def emit(row):print(json.dumps(row),flush=True)
def write(path,data):path.write_text(json.dumps(data))
def cmd(argv,cwd,log,env=None):
    start=time.time()
    with log.open('w') as out:r=subprocess.run(argv,cwd=cwd,stdout=out,stderr=subprocess.STDOUT,env=env,timeout=3600)
    return {'argv':argv,'seconds':time.time()-start,'returncode':r.returncode,'log_tail':log.read_text()[-2500:]}

def rfdiff(root,length,batch,seed):
    request={'schema':'fs2-serve.nebius.ai/scientific-run-request/v1','parameters':{'schema':'fs2-serve.nebius.ai/rfdiffusion-parameters/v1','operation':'design-backbone','contigs':[f'{length}-{length}'],'num_designs':batch,'seed':seed,'diffuser_T':50}}
    manifest={'schema':'fs2-serve.nebius.ai/scientific-artifact-manifest/v1','entries':[{'artifact':{'artifact_id':'artifact.rfdiffusion.base-ckpt','path':'rfdiffusion-base-checkpoint/Base_ckpt.pt','sha256':'0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca','size_bytes':483616107}}]}
    write(root/'request.json',request);write(root/'manifest.json',manifest)
    argv=['python','/opt/fs2/runtime_entrypoint.py','run','--request',str(root/'request.json'),'--input-manifest',str(root/'manifest.json'),'--output',str(root/'result'),'--scratch',str(root/'scratch'),'--cache-level','artifact-local']
    result=cmd(argv,root,root/'run.log')
    out=json.loads((root/'result/result.json').read_text())
    return {'request':request,'stages':[result],'output':out,'valid':out['status']=='succeeded'}

def complexa(root,target,batch,seed):
    run=root.name; config='/opt/fs2/source/configs/search_binder_local_pipeline.yaml'
    base=[config,'++run_name='+run,'++generation.task_name='+target,'++seed='+str(seed)]
    env=dict(os.environ,COMPLEXA_INIT='1',DATA_PATH='/opt/fs2/source/assets',AF2_DIR='/opt/fs2/artifacts/alphafold2-params',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    (root/'assets').symlink_to('/opt/fs2/source/assets',target_is_directory=True)
    stages=[]
    for stage in ['generate','filter','evaluate','analyze']:
        argv=['complexa',stage]+(['--verbose'] if stage=='filter' else [])+base
        if stage=='generate':argv+=['++ckpt_path=/opt/fs2/artifacts/complexa-protein','++ckpt_name=complexa.ckpt','++autoencoder_ckpt_path=/opt/fs2/artifacts/complexa-protein/complexa_ae.ckpt','++generation.args.nsteps=25','++generation.dataloader.dataset.nres.nsamples='+str(batch)]
        if stage=='filter':argv+=['++root_path=./inference/search_binder_local_pipeline_'+target+'_'+run]
        if stage=='evaluate':argv+=['++metric.compute_esm_metrics=false','++metric.compute_monomer_metrics=false','++metric.compute_designability=false','++metric.compute_codesignability=false','++metric.sequence_types=[self]']
        result=cmd(argv,root,root/(stage+'.log'),env);stages.append(result)
        if result['returncode']!=0:break
    files=[{'path':str(path.relative_to(root)),'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in root.rglob('*') if path.is_file() and not path.is_symlink() and path.suffix in ['.pdb','.cif','.csv']]
    return {'request':{'target':target,'samples':batch,'steps':25,'seed':seed},'stages':stages,'output_files':files,'valid':len(stages)==4 and all(s['returncode']==0 for s in stages) and any(f['path'].endswith('.csv') for f in files) and any(f['path'].endswith(('.pdb','.cif')) for f in files)}

p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--extended-complexa',action='store_true');p.add_argument('--only-case');p.add_argument('--only-repetition',type=int);a=p.parse_args()
emit({'type':'environment','model':a.model,'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,uuid,driver_version,memory.total,memory.used','--format=csv,noheader'],text=True)})
cases=[(80,1),(160,1),(256,1),(80,2)] if a.model=='rfdiffusion' else [('02_PDL1',1)]
if a.extended_complexa:cases=[('02_PDL1',2),('38_TNFalpha',1)]
if a.only_case:cases=[case for case in cases if str(case[0])==a.only_case]
attempt_id=uuid.uuid4().hex[:8]
for rep in range(1,4):
    if a.only_repetition and rep!=a.only_repetition:continue
    for case,batch in cases:
        root=pathlib.Path('/work')/f'{a.model}-{case}-b{batch}-r{rep}-{attempt_id}';root.mkdir()
        started=time.time()
        try:row=rfdiff(root,case,batch,42+rep) if a.model=='rfdiffusion' else complexa(root,case,batch,42+rep)
        except Exception as e:row={'valid':False,'error_type':type(e).__name__,'error':str(e)}
        row.update(type='attempt',model=a.model,case=case,batch=batch,repetition=rep,started_unix=started,validated_seconds=time.time()-started,cohort='new-process-local-image-shared-weights',artifact_root=str(root));write(root/'benchmark-row.json',row);emit(row)
emit({'type':'complete','model':a.model,'unix':time.time()})
