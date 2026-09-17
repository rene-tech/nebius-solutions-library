#!/usr/bin/env python3
import argparse
import copy
import json
from pathlib import Path
import urllib.request
import urllib.error
from benchmark_http import payload

parser = argparse.ArgumentParser()
parser.add_argument('--model', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--cases', type=Path, required=True)
args = parser.parse_args()
case = json.loads(args.cases.read_text())[0]
base = payload(args.model, case, 'contract-probe')
endpoint = '/biology/openfold/openfold2/predict-structure-from-msa-and-template' if args.model == 'openfold2' else '/biology/openfold/openfold3/predict'
probes = {}
if args.model == 'openfold2':
    for name, fields in {'msa': {'msa': '>query\n' + case['chains'][0]['sequence']}, 'templates': {'templates': []}, 'seed': {'seed': 43}, 'parameter_set': {'selected_models': [2]}, 'relaxation': {'relax_prediction': True}, 'batch': {'inputs': [base, base]}}.items():
        probes[name] = base | fields
else:
    for name, fields in {'templates': {'templates': []}, 'ligand': {'type': 'ligand', 'sequence': 'CCO'}, 'rna': {'type': 'rna', 'sequence': 'ACGU'}, 'dna': {'type': 'dna', 'sequence': 'ACGT'}, 'samples': {'diffusion_samples': 2}, 'seed': {'seed': 43}}.items():
        item = copy.deepcopy(base)
        item['inputs'][0]['molecules'][0].update(fields)
        probes[name] = item
    probes['batch'] = copy.deepcopy(base)
    probes['batch']['inputs'].append(copy.deepcopy(base['inputs'][0]))
rows = []
for name, value in probes.items():
    try:
        response = urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000' + endpoint, json.dumps(value).encode(), {'Content-Type': 'application/json'}), timeout=60)
        code, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read().decode()
    rows.append({'feature': name, 'http_status': code, 'response': json.loads(raw), 'expected': 'explicit rejection by current boundary'})
args.output.write_text(json.dumps(rows, indent=2) + '\n')
print(json.dumps(rows))
