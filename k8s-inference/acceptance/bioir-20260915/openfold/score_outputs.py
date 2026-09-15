#!/usr/bin/env python3
"""Paired CA lDDT/RMSD and reference quality; no biological efficacy claims."""
import io
import json
from pathlib import Path
import statistics

import numpy as np
from Bio.PDB import PDBParser, MMCIFParser
from Bio.PDB.MMCIF2Dict import MMCIF2Dict

ROOT = Path(__file__).resolve().parent
AA = dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(), 'ARNDCQEGHILKMFPSTWYV'))

def parse(text, fmt):
    if fmt == 'cif':
        # Native Preview2 omits occupancy, which Bio.PDB's full structure
        # parser requires. Read the actual atom-site coordinate table directly.
        data = MMCIF2Dict(io.StringIO(text))
        chains = {}
        seen = set()
        for i, atom in enumerate(data['_atom_site.label_atom_id']):
            residue = data['_atom_site.label_comp_id'][i]
            chain = data['_atom_site.label_asym_id'][i]
            key = (chain, data['_atom_site.label_seq_id'][i])
            if atom == 'CA' and residue in AA and key not in seen:
                chains.setdefault(chain, [[], []])[0].append(AA[residue])
                chains[chain][1].append([float(data['_atom_site.Cartn_' + axis][i]) for axis in 'xyz'])
                seen.add(key)
        return {chain: (''.join(value[0]), np.asarray(value[1])) for chain, value in chains.items()}
    parser = PDBParser(QUIET=True) if fmt == 'pdb' else MMCIFParser(QUIET=True, auth_chains=False, auth_residues=False)
    structure = parser.get_structure('model', io.StringIO(text))[0]
    chains = {}
    for chain in structure:
        seq, coords = [], []
        for residue in chain:
            if residue.resname in AA and 'CA' in residue:
                seq.append(AA[residue.resname])
                coords.append(residue['CA'].coord)
        if seq:
            chains[chain.id] = (''.join(seq), np.asarray(coords, dtype=np.float64))
    return chains

def coordinates(chains, case):
    # Native outputs preserve requested chain IDs. Enforce sequence identity.
    points = []
    for expected in case['chains']:
        sequence, coords = chains[expected['id']]
        if sequence != expected['sequence']:
            raise ValueError(f"Sequence mismatch on chain {expected['id']}: expected {len(expected['sequence'])}, got {len(sequence)}")
        points.append(coords)
    result = np.concatenate(points)
    if not np.isfinite(result).all():
        raise ValueError('Nonfinite coordinates')
    return result

def metrics(reference, candidate):
    assert reference.shape == candidate.shape
    dref = np.linalg.norm(reference[:, None] - reference[None, :], axis=-1)
    dcan = np.linalg.norm(candidate[:, None] - candidate[None, :], axis=-1)
    mask = (dref < 15.0) & (dref > 1e-8)
    error = np.abs(dref - dcan)[mask]
    lddt = float(np.mean([np.mean(error < cutoff) for cutoff in (0.5, 1, 2, 4)])) if error.size else None
    p, q = candidate - candidate.mean(axis=0), reference - reference.mean(axis=0)
    u, _, vt = np.linalg.svd(p.T @ q)
    sign = np.linalg.det(u @ vt)
    rotation = u @ np.diag([1, 1, sign]) @ vt
    rmsd = float(np.sqrt(np.mean(np.sum((p @ rotation - q) ** 2, axis=-1))))
    return {'ca_lddt': lddt, 'ca_rmsd_angstrom': rmsd, 'ca_atoms': len(reference), 'lddt_pairs': int(mask.sum()), 'lddt_cutoff_angstrom': 15.0}

def load_output(path, model):
    response = json.loads(path.read_text())
    result = response['structures_in_ranked_order'][0] if model == 'openfold2' else response['outputs'][0]['structures_with_scores'][0]
    return parse(result['structure'], result['format'])

def scores(path, model):
    response = json.loads(path.read_text())
    result = response['structures_in_ranked_order'][0] if model == 'openfold2' else response['outputs'][0]['structures_with_scores'][0]
    keys = ('confidence', 'ptm_score') if model == 'openfold2' else ('confidence_score', 'complex_plddt_score', 'complex_pde_score', 'ptm_score', 'iptm_score')
    return {key: result[key] for key in keys}

def main():
    cases = {c['id']: c for c in json.loads((ROOT / 'fixtures/cases.json').read_text())}
    rows = []
    for directory in sorted((ROOT / 'raw').iterdir()):
        if not directory.is_dir() or not (directory / 'attempts.jsonl').exists():
            continue
        model = directory.name.split('-')[0]
        gpu = directory.name.split('-')[-1]
        attempts = [json.loads(line) for line in (directory / 'attempts.jsonl').read_text().splitlines()]
        for attempt in attempts:
            if not attempt.get('valid'):
                continue
            case = cases[attempt['case']]
            row = {'resource': directory.name, 'model': model, 'gpu': gpu, 'case': case['id'], 'request_id': attempt['request_id'], 'cohort': attempt['cohort']}
            try:
                predicted = coordinates(load_output(directory / (attempt['request_id'] + '.response.json'), model), case)
                row['sequence_and_finite_coordinates_valid'] = True
                row['native_confidence_scores'] = scores(directory / (attempt['request_id'] + '.response.json'), model)
                if 'reference_url' in case:
                    reference = coordinates(parse((ROOT / 'fixtures' / (case['id'] + '.pdb')).read_text(), 'pdb'), case)
                    row['reference'] = metrics(reference, predicted)
                baseline = ROOT / 'raw' / f'{model}-baseline-{gpu}' / f'{model}-baseline-{case["id"]}-warm-1.response.json'
                if baseline.exists():
                    row['paired_to_baseline'] = metrics(coordinates(load_output(baseline, model), case), predicted)
                    baseline_scores = scores(baseline, model)
                    row['native_confidence_delta_vs_baseline'] = {key: value - baseline_scores[key] for key, value in row['native_confidence_scores'].items()}
                resident = ROOT / 'raw' / f'{model}-upstream-resident-{gpu}' / f'{model}-upstream-resident-{case["id"]}-warm-1.response.json'
                if '-graph-resident-' in directory.name and resident.exists():
                    row['paired_to_same_gpu_upstream_resident'] = metrics(coordinates(load_output(resident, model), case), predicted)
            except Exception as exc:
                row['sequence_and_finite_coordinates_valid'] = False
                row['error'] = f'{type(exc).__name__}: {exc}'
            rows.append(row)
    output = {'metric': 'CA-only lDDT with 15A neighborhood and 0.5/1/2/4A thresholds; Kabsch CA RMSD', 'limits': ['Small public convenience corpus; not broad accuracy qualification.', 'Same seed does not guarantee matched diffusion noise across different implementations.', 'Repeated requests retain the same native seed; they are timing repeats, not independent Monte Carlo quality samples.', 'Protein-complex metric includes CA distances but is not DockQ.', 'Reference bound-state structures can differ from unbound predictions.'], 'samples': rows}
    (ROOT / 'quality.json').write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'samples': len(rows), 'valid': sum(r.get('sequence_and_finite_coordinates_valid', False) for r in rows)}))

if __name__ == '__main__':
    main()
