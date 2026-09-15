#!/usr/bin/env python3
"""Retain current primary public source excerpts and full-file hashes."""
import hashlib,json,pathlib,urllib.request,concurrent.futures
from control import ROOT,save

sources={
'bir-support':'https://raw.githubusercontent.com/NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/main/docs/ref/support-matrix.md',
'bir-hubs':'https://raw.githubusercontent.com/NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/main/bionemo_ir/hubs.py',
'rfdiffusion-attention':'https://raw.githubusercontent.com/RosettaCommons/RFdiffusion/9273ef67335acaf91df0150473a274759229cdf6/rfdiffusion/Attention_module.py',
'rfdiffusion-track':'https://raw.githubusercontent.com/RosettaCommons/RFdiffusion/9273ef67335acaf91df0150473a274759229cdf6/rfdiffusion/Track_module.py',
'complexa-readme':'https://raw.githubusercontent.com/NVIDIA-BioNeMo/Proteina-Complexa/54058860d43444c7289873f77d3e50b5b02348cd/README.md',
'genmol-modelcard':'https://raw.githubusercontent.com/NVIDIA-BioNeMo/genmol/main/MODEL_CARD.md',
'evo2-readme':'https://raw.githubusercontent.com/ArcInstitute/evo2/main/README.md',
'proteinmpnn-source':'https://raw.githubusercontent.com/dauparas/ProteinMPNN/main/protein_mpnn_utils.py',
'diffdock-readme':'https://raw.githubusercontent.com/gcorso/DiffDock/main/README.md',
'diffdock-score':'https://raw.githubusercontent.com/gcorso/DiffDock/main/models/score_model.py',
'bir-commit':'https://api.github.com/repos/NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/commits/main',
'complexa-tree':'https://api.github.com/repos/NVIDIA-BioNeMo/Proteina-Complexa/git/trees/54058860d43444c7289873f77d3e50b5b02348cd?recursive=1',
}
def fetch(item):
    name,url=item
    try:
        raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'FS2-bioir-evaluation'}),timeout=30).read()
        (ROOT/'sources').mkdir(exist_ok=True)
        path=ROOT/'sources'/(name+'.txt');path.write_bytes(raw)
        return {'id':name,'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'path':str(path.relative_to(ROOT))}
    except Exception as e:return {'id':name,'url':url,'error':str(e)}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:rows=list(pool.map(fetch,sources.items()))
save('sources/index.json',rows)
for row in rows:print(json.dumps(row))
