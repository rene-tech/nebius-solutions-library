#!/usr/bin/env python3
"""Retain version pins with an explicit distinction between metadata and hashing."""
import hashlib,json
from control import ROOT,k,save
repo=ROOT.parents[2]
protein=repo/'catalog/runtime/deployment-runtimes/artifacts/proteinmpnn-0b4230fdc15937b8733f208872dd328dd5715016866543ee76cc475a32f37bbf.json'
lock=repo/'models/cancer-immunotherapy/runtime-images/proteina-complexa/image-lock.json'
save('inventory/proteinmpnn-checkpoint-pin.json',{'provenance':'Current catalog deployment-runtime artifact manifest; immutable benchmark image retained in lifecycle. No additional hash read from live customer pod.','file':str(protein.relative_to(repo)),'sha256':hashlib.sha256(protein.read_bytes()).hexdigest(),'manifest':json.loads(protein.read_text())})
save('inventory/proteina-complexa-image-lock.json',json.loads(lock.read_text()))
name='fs2-bioir-coverage-proteina-complexa';pod=json.loads(k('-n','fs2-bioir-coverage','get','pod',name,'-o','json'))
assert pod['metadata']['labels']['lane']=='coverage' and pod['metadata']['labels']['evaluation']=='fs2-bioir-20260915'
script='import hashlib,json,pathlib; paths=[pathlib.Path("/opt/fs2/artifacts/complexa-protein/complexa.ckpt"),pathlib.Path("/opt/fs2/artifacts/complexa-protein/complexa_ae.ckpt")]; rows=[]\nfor p in paths:\n h=hashlib.sha256()\n with p.open("rb") as f:\n  for chunk in iter(lambda:f.read(8*1024*1024),b""):h.update(chunk)\n rows.append({"path":str(p),"bytes":p.stat().st_size,"sha256":h.hexdigest()})\nprint(json.dumps(rows))'
rows=json.loads(k('-n','fs2-bioir-coverage','exec',name,'--','python','-c',script))
expected={f['path']:f['sha256'] for f in json.loads(lock.read_text())['external_artifacts'][0]['files']}
assert all(row['sha256']==expected[row['path'].split('/')[-1]] for row in rows)
save('inventory/proteina-complexa-actual-weight-hashes.json',{'method':'Full SHA256 read after all benchmark requests; source mount read-only','files':rows})
print('PROVENANCE VERIFIED',flush=True)
