#!/usr/bin/env python3
"""In-pod benchmark: preserve every attempt and actual full HTTP response."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request
import urllib.error

AA = dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(), 'ARNDCQEGHILKMFPSTWYV'))

def validate(model, case, response):
    if model == 'openfold2':
        records = response['structures_in_ranked_order']
        assert len(records) == 1
        result = records[0]
        seq = ''.join(AA[line[17:20]] for line in result['structure'].splitlines() if line.startswith('ATOM  ') and line[12:16].strip() == 'CA')
        assert seq == case['chains'][0]['sequence'], 'sequence mismatch'
        for row in result['predicted_aligned_error']:
            assert all(math.isfinite(x) for x in row)
        assert len(result['plddt']) == len(seq)
        assert all(math.isfinite(x) for x in result['plddt'])
        return {'residues': len(seq), 'confidence': result['confidence'], 'ptm': result['ptm_score'], 'server_seconds': result['inference_seconds']}
    records = response['outputs'][0]['structures_with_scores']
    assert len(records) == 1
    result = records[0]
    assert '_atom_site.Cartn_x' in result['structure']
    assert all(math.isfinite(result[k]) for k in ('confidence_score', 'complex_plddt_score', 'complex_pde_score', 'ptm_score', 'iptm_score'))
    return {'residues_expected': sum(len(c['sequence']) for c in case['chains']), 'confidence': result['confidence_score'], 'ptm': result['ptm_score'], 'server_seconds': response['outputs'][0]['runtime_metrics']['inference_seconds']}

def payload(model, case, request_id):
    if model == 'openfold2':
        return {'input_id': request_id, 'sequence': case['chains'][0]['sequence'], 'selected_models': [1], 'relax_prediction': False}
    molecules = []
    for chain in case['chains']:
        m = {'type': 'protein', 'id': chain['id'], 'sequence': chain['sequence'], 'diffusion_samples': 1}
        if 'msa' in chain:
            m['msa'] = {'main': {'a3m': {'alignment': chain['msa'], 'format': 'a3m'}}}
        molecules.append(m)
    return {'request_id': request_id, 'inputs': [{'input_id': request_id, 'output_format': 'cif', 'molecules': molecules}]}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['openfold2', 'openfold3'], required=True)
    parser.add_argument('--variant', default='baseline')
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--stop-after-failure', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text())
    if args.model == 'openfold2':
        cases = [c for c in cases if c['feature'] == 'monomer']
    endpoint = '/biology/openfold/openfold2/predict-structure-from-msa-and-template' if args.model == 'openfold2' else '/biology/openfold/openfold3/predict'
    identity = json.loads(urllib.request.urlopen(args.base_url + '/v1/runtime').read())
    (args.output / 'runtime.json').write_text(json.dumps(identity, indent=2) + '\n')
    def attempt(case, cohort, index):
        request_id = f'{args.model}-{args.variant}-{case["id"]}-{cohort}-{index}'
        request = payload(args.model, case, request_id)
        body = json.dumps(request).encode()
        row = {'request_id': request_id, 'case': case['id'], 'variant': args.variant, 'cohort': cohort, 'utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'input_sha256': hashlib.sha256(body).hexdigest(), 'input_bytes': len(body)}
        (args.output / f'{request_id}.request.json').write_bytes(body)
        started = time.perf_counter()
        try:
            response = urllib.request.urlopen(urllib.request.Request(args.base_url + endpoint, body, {'Content-Type': 'application/json'}), timeout=1800)
            raw = response.read()
            row['http_seconds'] = time.perf_counter() - started
            row['status'] = response.status
            parsed = json.loads(raw)
            (args.output / f'{request_id}.response.json').write_bytes(raw)
            row['validation'] = validate(args.model, case, parsed)
            row['valid'] = True
        except Exception as exc:
            row['valid'] = False
            row['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, urllib.error.HTTPError):
                row['status'] = exc.code
                (args.output / f'{request_id}.response.json').write_bytes(exc.read())
        row['wall_to_validated_artifact_seconds'] = time.perf_counter() - started
        with (args.output / 'attempts.jsonl').open('a') as handle:
            handle.write(json.dumps(row) + '\n')
        print(json.dumps(row), flush=True)
        return row
    for case in cases:
        first = attempt(case, 'first_shape', 0)
        if args.stop_after_failure and not first['valid']:
            return
        for i in range(args.repetitions):
            attempt(case, 'warm', i + 1)
    # Current adapters serialize under a runtime lock. Measure accepted concurrent
    # requests including queue time; this is not represented as true tensor batching.
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda c: attempt(c, 'mixed_concurrent', 0), cases[:3]))
    summary = {'seconds': time.perf_counter() - started, 'attempts': len(rows), 'valid': sum(r['valid'] for r in rows), 'concurrency': 3, 'meaning': 'HTTP concurrent queue, not tensor batching'}
    (args.output / 'mixed-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    final_identity = json.loads(urllib.request.urlopen(args.base_url + '/v1/runtime').read())
    (args.output / 'runtime-after.json').write_text(json.dumps(final_identity, indent=2) + '\n')

if __name__ == '__main__':
    main()
