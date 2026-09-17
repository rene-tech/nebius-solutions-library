#!/usr/bin/env python3
"""Prepare pinned public fixture cohort and task-owned client ConfigMaps."""
import copy
import hashlib
import json
import pathlib
import subprocess
import urllib.request
from control import K,ROOT,save

REPO=ROOT.parents[2]
FIX=REPO/'catalog/runtime/packaged-repository/nim-fast-start/faststart-v2'
cases={}
aa={'ALA':'A','ARG':'R','ASN':'N','ASP':'D','CYS':'C','GLU':'E','GLN':'Q','GLY':'G','HIS':'H','ILE':'I','LEU':'L','LYS':'K','MET':'M','PHE':'F','PRO':'P','SER':'S','THR':'T','TRP':'W','TYR':'Y','VAL':'V'}
pdbs={};seqs={}
for code in ['1UBQ','1LYZ','1MBN']:
    if code=='1UBQ':raw=json.loads((FIX/'proteinmpnn-native/fixtures/1ubq-request.json').read_text())['input_pdb']
    else:raw=urllib.request.urlopen('https://files.rcsb.org/download/'+code+'.pdb').read().decode()
    lines=[l for l in raw.splitlines() if l.startswith('ATOM  ') and l[21]=='A' and l[16] in [' ','A']]
    if not lines:lines=[l for l in raw.splitlines() if l.startswith('ATOM  ') and l[16] in [' ','A']]
    pdbs[code]='\n'.join(lines)+'\nTER\nEND\n'
    seqs[code]=''.join(aa[l[17:20]] for l in lines if l[12:16].strip()=='CA')
save('fixtures/structures.json',{'source':'https://files.rcsb.org/download/{code}.pdb','structures':{code:{'pdb':pdbs[code],'sequence':seqs[code],'sha256':hashlib.sha256(pdbs[code].encode()).hexdigest()} for code in pdbs}})
cases['proteinmpnn']=[{'name':f'{code}-{len(seqs[code])}aa','endpoint':'/biology/ipd/proteinmpnn/predict','payload':{'input_pdb':pdbs[code],'num_seq_per_target':1,'random_seed':42}} for code in pdbs]
cases['proteinmpnn'].append({'name':'1UBQ-native-batch4','endpoint':'/biology/ipd/proteinmpnn/predict','payload':{'input_pdb':pdbs['1UBQ'],'num_seq_per_target':4,'random_seed':42}})
diff=json.loads((FIX/'diffdock-native/fixtures/1ubq-aspirin-request.json').read_text())
cases['diffdock']=[]
for code in pdbs:
    payload=copy.deepcopy(diff);payload['protein']=pdbs[code]
    cases['diffdock'].append({'name':f'{code}-aspirin','endpoint':'/molecular-docking/diffdock/generate','payload':payload})
gen=json.loads((FIX/'genmol-native/fixtures/requests-qed-logp.json').read_text())['calls']
cases['genmol']=[dict(c,endpoint='/generate') for c in gen]
large=copy.deepcopy(gen[0]);large['name']='qed-60tokens-batch8';large['payload'].update(smiles='[*{50-70}]',num_molecules=8);cases['genmol'].append(dict(large,endpoint='/generate'))
mol=json.loads((FIX/'molmim-native/fixtures/request-cmaes-qed.json').read_text())['cases']
cases['molmim']=[dict(c,endpoint='/generate') for c in mol]
large=copy.deepcopy(mol[1]);large['name']='aspirin-particles8-iterations4-batch4';large['payload'].update(particles=8,iterations=4,num_molecules=4);cases['molmim'].append(dict(large,endpoint='/generate'))
msa=json.loads((FIX/'msa-search-native/fixtures/request-pdb70.json').read_text())
cases['msa-search-pdb70']=[{'name':f'{code}-{len(seqs[code])}aa','endpoint':'/biology/colabfold/msa-search/predict','payload':dict(msa,sequence=seqs[code])} for code in seqs]
save('fixtures/cases.json',cases)
cases['evo2-40b']=[{'name':f'synthetic-{length}nt-output{tokens}','endpoint':'/biology/arc/evo2/generate','payload':{'sequence':('ATCGGCTAACGT'*((length+11)//12))[:length],'num_tokens':tokens,'temperature':0.7,'top_k':1,'top_p':0.0,'random_seed':42,'enable_logits':False,'enable_sampled_probs':False,'enable_elapsed_ms_per_token':True}} for length,tokens in [(64,32),(512,64),(2048,128)]]
save('fixtures/cases.json',cases)
ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':'fs2-bioir-coverage','labels':{'evaluation':'fs2-bioir-20260915','lane':'coverage'}}}
save('manifests/namespace.json',ns)
subprocess.run(K+['apply','--dry-run=client','-f',str(ROOT/'manifests/namespace.json')],check=True)
subprocess.run(K+['apply','-f',str(ROOT/'manifests/namespace.json')],check=True)
for namespace in ['fs2-bioir-coverage','fs2-models']:
    cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fs2-bioir-coverage-client','namespace':namespace,'labels':{'evaluation':'fs2-bioir-20260915','lane':'coverage'}},'data':{'client.py':(ROOT/'client.py').read_text(),'cases.json':json.dumps(cases)}}
    path=ROOT/'manifests'/f'client-{namespace}.json';save(str(path.relative_to(ROOT)),cm)
    subprocess.run(K+['apply','--dry-run=client','-f',str(path)],check=True)
    subprocess.run(K+['apply','--server-side','-f',str(path)],check=True)
print({m:[c['name'] for c in cs] for m,cs in cases.items()})
