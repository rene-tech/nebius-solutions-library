#!/usr/bin/env python3
"""One explicit validated request schedule; no hidden smoke or repeats."""
import argparse,hashlib,json,math,pathlib,time,urllib.request
from benchmark_http import quality
p=argparse.ArgumentParser();p.add_argument('--cases',required=True);p.add_argument('--out',required=True);p.add_argument('--samples',type=int,default=1);a=p.parse_args()
out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
for index,name in enumerate(a.cases.split(',')):
    request=json.loads(pathlib.Path('/models/fixtures',name+'.json').read_text());request['diffusion_samples']=a.samples
    row={'fixture':name,'samples':a.samples,'index':index,'request':request,'started_unix':time.time()};start=time.perf_counter()
    try:
        payload=json.dumps(request).encode();row['input_sha256']=hashlib.sha256(payload).hexdigest()
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/biology/mit/boltz2/predict',data=payload,headers={'Content-Type':'application/json'}),timeout=1200) as response:result=json.load(response)
        row['http_seconds']=time.perf_counter()-start
        (out/(str(index)+'-'+name+'.response.json')).write_text(json.dumps(result))
        assert len(result['structures'])==a.samples
        assert all(math.isfinite(x) and 0<=x<=1 for x in result['confidence_scores']+result['ptm_scores'])
        row['quality']=[]
        for sample,structure in enumerate(result['structures']):
            cif=structure['structure'];(out/f'{index}-{name}-{sample}.cif').write_text(cif)
            reference=pathlib.Path('/models/fixtures',name+'.pdb')
            if not reference.exists():reference=reference.with_suffix('.cif')
            row['quality'].append(quality(cif,str(reference),sum(len(v['sequence']) for v in request['polymers'])))
        row.update(valid=True,confidence_scores=result['confidence_scores'],ptm_scores=result['ptm_scores'])
    except Exception as e:row.update(valid=False,error_type=type(e).__name__,error=str(e))
    row['validated_seconds']=time.perf_counter()-start
    with (out/'attempts.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    print(json.dumps(row),flush=True)
