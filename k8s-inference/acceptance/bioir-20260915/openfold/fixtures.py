#!/usr/bin/env python3
"""Freeze public PDB structures plus the public BioIR CASP14 MSA fixture."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent
AA = dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(), 'ARNDCQEGHILKMFPSTWYV'))

def main():
    target = ROOT / 'fixtures'
    target.mkdir(exist_ok=True)
    cases = []
    for pdb_id in ('1crn', '1lyz', '1ake', '4hhb'):
        url = f'https://files.rcsb.org/download/{pdb_id.upper()}.pdb'
        raw = (target / f'{pdb_id}.pdb').read_bytes() if (target / f'{pdb_id}.pdb').exists() else urllib.request.urlopen(url, timeout=60).read()
        (target / f'{pdb_id}.pdb').write_bytes(raw)
        chains = {}
        seen = set()
        for line in raw.decode().splitlines():
            if line.startswith('ATOM  ') and line[12:16].strip() == 'CA' and line[16] in ' A' and line[17:20] in AA:
                key = (line[21], line[22:27])
                if key not in seen:
                    chains.setdefault(line[21], []).append(AA[line[17:20]])
                    seen.add(key)
        chain_ids = ('A', 'B') if pdb_id == '4hhb' else ('A',)
        cases.append({'id': pdb_id, 'reference_url': url, 'reference_sha256': hashlib.sha256(raw).hexdigest(), 'chains': [{'id': key, 'sequence': ''.join(chains[key])} for key in chain_ids], 'feature': 'protein_complex' if pdb_id == '4hhb' else 'monomer'})
    vendor = ROOT.parent / 'boltz2/vendor/bioir/examples/data/samples/monomers'
    source = json.loads((vendor / 'T1031.json').read_text())[0]
    msa = (vendor / 'msas/T1031.a3m').read_text()
    cases.append({'id': 'T1031_msa', 'source_commit': '401c6fcc4a43925bcf1342b6c0979b060130b396', 'chains': [{'id': 'A', 'sequence': source['polymers'][0]['sequence'], 'msa': msa}], 'feature': 'main_msa', 'msa_sha256': hashlib.sha256(msa.encode()).hexdigest()})
    truth = (vendor.parent / 'gt/T1031.pdb').read_bytes()
    (target / 'T1031_msa.pdb').write_bytes(truth)
    cases[-1].update({'reference_url': 'https://github.com/NVIDIA-BioNeMo/BioNeMo-Inference-Runtime/blob/401c6fcc4a43925bcf1342b6c0979b060130b396/examples/data/samples/gt/T1031.pdb', 'reference_sha256': hashlib.sha256(truth).hexdigest()})
    (target / 'cases.json').write_text(json.dumps(cases, indent=2) + '\n')
    print([(c['id'], [len(ch['sequence']) for ch in c['chains']]) for c in cases])

if __name__ == '__main__':
    main()
