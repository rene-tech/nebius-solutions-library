#!/usr/bin/env python3
"""Independently validate every retained local PDB70 alignment without reruns."""
import hashlib,json
from control import ROOT,save
rows=[]
for path in sorted((ROOT/'raw').glob('msa-search-pdb70*.jsonl')):
    for line in path.read_text().splitlines():
        request=json.loads(line)
        if request.get('type')!='attempt' or not request.get('valid'):continue
        records=[];alignment=request['output']['alignments']['pdb70_220313']['a3m']['alignment'];sequence=request['payload']['sequence']
        for line in alignment.splitlines():
            if line.startswith('>'):records.append([line[1:],''])
            elif line.strip():records[-1][1]+=line.strip()
        columns=[len(''.join(c for c in seq if c.isupper() or c=='-')) for _,seq in records]
        assert records[0][1]==sequence and all(length==len(sequence) for length in columns)
        assert 1<len(records)<=min(128,request['payload']['max_msa_sequences'])
        assert all(set(seq)<=set('ACDEFGHIKLMNPQRSTVWY-abcdefghijklmnopqrstuvwxyz') for _,seq in records)
        rows.append({'source':str(path.relative_to(ROOT)),'case':request['case'],'cohort':request['cohort'],'repetition':request['repetition'],'records':len(records),'aligned_columns':len(sequence),'query_identical':True,'alignment_sha256':hashlib.sha256(alignment.encode()).hexdigest(),'valid':True})
save('raw/msa-independent-validation.json',{'valid':all(r['valid'] for r in rows),'attempts':rows,'note':'Validates actual homolog alignment structure, not biological efficacy or comparison against another database.'})
print('Validated',len(rows),'MSA outputs')
