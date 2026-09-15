#!/usr/bin/env python3
import hashlib,json,urllib.request
from control import ROOT,save
index=json.loads((ROOT/'sources/index.json').read_text())
base='https://raw.githubusercontent.com/'
items={
'complexa-transformer':base+'NVIDIA-BioNeMo/Proteina-Complexa/54058860d43444c7289873f77d3e50b5b02348cd/src/proteinfoundation/nn/protein_transformer.py',
'complexa-af2-reward':base+'NVIDIA-BioNeMo/Proteina-Complexa/54058860d43444c7289873f77d3e50b5b02348cd/src/proteinfoundation/rewards/alphafold2_reward.py',
'complexa-latents':base+'NVIDIA-BioNeMo/Proteina-Complexa/54058860d43444c7289873f77d3e50b5b02348cd/src/proteinfoundation/nn/local_latents_transformer_v2.py',
'diffdock-score-pinned':base+'gcorso/DiffDock/85c49b60d3e0b0182a59ee43a34a6d7036981284/models/score_model.py',
'bir-opm-dispatch':base+'NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/401c6fcc4a43925bcf1342b6c0979b060130b396/bionemo_ir/_torch/custom_ops/outer_product_mean/ops.py',
'bir-opm-config':base+'NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/401c6fcc4a43925bcf1342b6c0979b060130b396/bionemo_ir/_torch/custom_ops/outer_product_mean/_config.py',
'bir-opm-cutedsl':base+'NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/401c6fcc4a43925bcf1342b6c0979b060130b396/bionemo_ir/_torch/custom_ops/outer_product_mean/cutedsl.py',
}
for name,url in items.items():
    try:
        raw=urllib.request.urlopen(url,timeout=30).read();path=ROOT/'sources'/(name+'.txt');path.write_bytes(raw)
        row={'id':name,'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'path':str(path.relative_to(ROOT))}
    except Exception as e:row={'id':name,'url':url,'error':str(e)}
    index.append(row);print(json.dumps(row))
save('sources/index.json',index)
