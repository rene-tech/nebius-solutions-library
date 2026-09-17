#!/usr/bin/env python3
"""In-pod, loopback end-to-end baseline measurement, preserving every attempt."""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
import uuid

def emit(record):
    print(json.dumps(record, allow_nan=False), flush=True)

def http(path,payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request('http://127.0.0.1:8000'+path,data=data,headers={'Content-Type':'application/json','X-Request-ID':'bioir-'+uuid.uuid4().hex})
    with urllib.request.urlopen(req,timeout=1800) as resp:
        return json.load(resp)

def validate(model,payload,out):
    if model=='proteinmpnn':
        records=[]
        for line in out['mfasta'].splitlines():
            if line.startswith('>'): records.append(['',line[1:]])
            else: records[-1][0]+=line.strip()
        assert len(records)==payload['num_seq_per_target']+1
        source=records[0][0]; scores=out['scores']; probs=out['probs']
        assert len(scores)==len(probs)==payload['num_seq_per_target']
        for i,(seq,header) in enumerate(records[1:]):
            assert len(seq)==len(source) and set(seq)<=set('ACDEFGHIKLMNPQRSTVWY')
            assert math.isfinite(scores[i]) and scores[i]>=0
            assert len(probs[i])==len(source)
            for row in probs[i]:
                assert len(row)==21 and all(math.isfinite(x) and 0<=x<=1 for x in row)
                assert abs(sum(row)-1)<1e-4
        return {'valid_sequences':len(records)-1,'length':len(source),'scores':scores,'sequence_recovery':[sum(a==b for a,b in zip(source,s))/len(source) for s,h in records[1:]],'seed_in_header':f"seed={payload['random_seed']}" in records[0][1]}
    if model in ['genmol','molmim']:
        from rdkit import Chem
        from rdkit.Chem import QED, Crippen
        values=out.get('molecules') or out.get('generated')
        if values is None and 'generated' in out: values=json.loads(out['generated'])
        assert values
        results=[]
        for value in values:
            if isinstance(value,str): smiles=value
            else: smiles=value.get('smiles') or value.get('smi') or value.get('molecule') or value.get('sample')
            mol=Chem.MolFromSmiles(smiles);assert mol is not None and mol.GetNumAtoms()>0
            results.append({'smiles':Chem.MolToSmiles(mol),'qed':QED.qed(mol),'logp':Crippen.MolLogP(mol)})
        return {'valid_molecules':len(results),'requested_molecules':payload['num_molecules'],'molecules':results}
    if model=='msa-search-pdb70':
        text=json.dumps(out)
        assert payload['sequence'] in text
        return {'query_present':True,'keys':sorted(out),'response_bytes':len(text),'structural_validation_only':True}
    if model=='diffdock':
        from rdkit import Chem
        text=json.dumps(out)
        sd=out.get('ligand_positions') or out.get('sdf') or out.get('output')
        if sd is None: sd=next((v for v in out.values() if isinstance(v,str) and 'V2000' in v),None)
        blocks=sd if isinstance(sd,list) else [sd]
        assert blocks and all(isinstance(b,str) for b in blocks)
        molecules=[Chem.MolFromMolBlock(b,sanitize=True,removeHs=False) for b in blocks]
        assert all(m is not None and m.GetNumConformers()>0 for m in molecules)
        return {'valid_poses':len(molecules),'atoms':[m.GetNumAtoms() for m in molecules],'keys':sorted(out),'no_native_pose_accuracy_claim':True}
    if model=='evo2-40b':
        seq=out['sequence'];assert len(seq)==payload['num_tokens'] and set(seq)<=set('ACGTN')
        timings=out['elapsed_ms_per_token'];assert len(timings)==payload['num_tokens'] and all(math.isfinite(x) and x>0 for x in timings)
        assert out['logits'] is None and out['sampled_probs'] is None
        return {'keys':sorted(out),'generated_bases':len(seq),'sequence_sha256':hashlib.sha256(seq.encode()).hexdigest(),'backend_elapsed_ms':out['elapsed_ms'],'per_token_ms':timings,'no_biological_quality_claim':True}
    raise ValueError(model)

def run(model,case,repetition,cohort):
    started=time.time(); begin=time.perf_counter()
    row={'type':'attempt','model':model,'case':case['name'],'repetition':repetition,'cohort':cohort,'request_started_unix':started,'input_sha256':hashlib.sha256(json.dumps(case['payload'],sort_keys=True).encode()).hexdigest(),'payload':case['payload']}
    try:
        out=http(case['endpoint'],case['payload']); received=time.perf_counter()
        row.update(http_seconds=received-begin,output=out,validation=validate(model,case['payload'],out),valid=True)
    except Exception as exc:
        row.update(valid=False,error_type=type(exc).__name__,error=str(exc))
        if isinstance(exc,urllib.error.HTTPError):row['error_body']=exc.read().decode(errors='replace')[:2000]
    row['validated_seconds']=time.perf_counter()-begin
    emit(row)

def main():
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);a=p.parse_args();model=a.model
    cases=json.load(open('/eval/cases.json'))[model]
    start=time.time();emit({'type':'client_start','unix':start,'model':model})
    for cmd in [['nvidia-smi','--query-gpu=name,uuid,driver_version,memory.total,memory.used,utilization.gpu','--format=csv,noheader'],['nvidia-smi','--query-compute-apps=pid,process_name,used_gpu_memory','--format=csv,noheader']]:
        try:
            result=subprocess.run(cmd,text=True,capture_output=True);emit({'type':'environment','command':cmd,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        except FileNotFoundError:emit({'type':'environment','command':cmd,'unavailable':True})
    try:
        import torch
        emit({'type':'torch','version':torch.__version__,'cuda':torch.version.cuda,'devices':torch.cuda.device_count(),'capability':torch.cuda.get_device_capability() if torch.cuda.is_available() else None})
    except ImportError:pass
    deadline=time.time()+1800
    while True:
        try:health=http('/v1/health/ready');break
        except Exception:
            if time.time()>deadline:raise
            time.sleep(1)
    emit({'type':'ready','health':health,'wait_seconds':time.time()-start,'unix':time.time()})
    if model=='evo2-40b':emit({'type':'runtime_identity','value':http('/v1/runtime')})
    for case in cases:run(model,case,0,'first-shape')
    for repetition in range(1,4):
        for case in cases:run(model,case,repetition,'warm-sequential')
    for repetition in range(1,4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda c:run(model,c,repetition,'mixed-concurrent-2'),cases))
    emit({'type':'complete','model':model,'client_elapsed_seconds':time.time()-start,'unix':time.time()})

if __name__=='__main__':main()
