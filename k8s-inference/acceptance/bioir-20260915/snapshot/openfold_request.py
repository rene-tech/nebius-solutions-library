#!/usr/bin/env python3
import argparse,json,pathlib,time,urllib.request
from benchmark_http import payload,validate
p=argparse.ArgumentParser();p.add_argument('--cases',required=True);p.add_argument('--out',required=True);p.add_argument('--samples',type=int,default=1);a=p.parse_args();out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
fixtures={c['id']:c for c in json.loads(pathlib.Path('/snapshot-app/cases.json').read_text())}
for index,name in enumerate(a.cases.split(',')):
    case=fixtures[name];request=payload('openfold2',case,'snapshot-'+name+'-'+str(index));row={'fixture':name,'request':request,'started_unix':time.time()};start=time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/biology/openfold/openfold2/predict-structure-from-msa-and-template',data=json.dumps(request).encode(),headers={'Content-Type':'application/json'}),timeout=1200) as response:result=json.load(response)
        row['http_seconds']=time.perf_counter()-start;(out/f'{index}-{name}.response.json').write_text(json.dumps(result));row.update(valid=True,quality=validate('openfold2',case,result))
    except Exception as e:row.update(valid=False,error_type=type(e).__name__,error=str(e))
    row['validated_seconds']=time.perf_counter()-start
    with (out/'attempts.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    print(json.dumps(row),flush=True)
