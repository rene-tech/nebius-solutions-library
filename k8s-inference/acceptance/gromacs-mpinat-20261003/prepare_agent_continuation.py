"""Stage the exact 18 unexecuted r5 cases; never submit or replay simulation."""
import argparse
import copy
from pathlib import Path

from run_agent_cases import COMPLETED, IMAGE, R5_IMAGE, R5_REMAINING
from stage_agent_inputs import copy_checked, load, save_new, sha

MEM_OPERATION = '7c59f87a-d62c-46aa-bd1c-14cb1a5cf0e6'
R5_SOURCE = '61b4c3bf6c3412746e76afc6ce3bdc1521b9a8c6'
REMAINING = R5_REMAINING


def select(manifest, verification):
    if (manifest['candidate_image'] != IMAGE or verification.get('operation_id') != MEM_OPERATION
            or verification.get('native_report_verified') is not True
            or verification.get('verified_repeats') != 3):
        raise ValueError('Require exact retained r4 cohort and verified completed MEM evidence')
    cases = copy.deepcopy(manifest['cases'])
    names = [case['case_id'] for case in cases]
    if len(names) != len(set(names)) or set(names) != REMAINING | {'mpinat-benchmem'}:
        raise ValueError('Original remaining cohort differs; do not infer an unexecuted set')
    return [case for case in cases if case['case_id'] in REMAINING]


def prepare(args):
    previous, manifest, proof = load(args.previous), load(args.manifest), load(args.verification)
    cases = select(manifest, proof)
    if args.output.exists() or (args.workspace / 'inputs/mpinat').exists():
        raise FileExistsError('Preserve all original staging and candidate inputs')
    old, new = IMAGE.split(':')[-1][:8], R5_IMAGE.split(':')[-1][:8]
    files, parameters, sizes = [], [], {}
    recorded_parameters = {row['case_id']: row for row in previous['parameters']}
    for case in cases:
        name = case['case_id'].removeprefix('mpinat-')
        rows = [row for row in previous['files'] if '/inputs/mpinat/' + name + '/' in row['target']]
        if {Path(row['target']).name for row in rows} != {'input.tar.gz', 'provenance.json'}:
            raise ValueError('Missing exact public input/provenance binding')
        for row in rows:
            target = args.workspace / 'inputs/mpinat' / name / Path(row['target']).name
            files.append(copy_checked(Path(row['target']), target, row['sha256'], row['size_bytes']))
            if target.name == 'input.tar.gz':
                sizes[case['case_id']] = row['size_bytes']
        recorded = recorded_parameters[case['case_id']]
        source = Path(recorded['target'])
        if sha(source) != recorded['sha256']:
            raise ValueError('Earlier prepared parameter bytes changed')
        original = load(source)
        revised = {**original, 'output_prefix': original['output_prefix'].replace('candidate-' + old, 'candidate-' + new)}
        if revised['output_prefix'] == original['output_prefix']:
            raise ValueError('Expected the exact old isolated output prefix')
        target = args.workspace / 'inputs/mpinat' / name / 'parameters-candidate.json'
        save_new(target, revised)
        parameters.append({'case_id': case['case_id'], 'source': str(source),
                           'source_sha256': recorded['sha256'], 'target': str(target), 'sha256': sha(target),
                           'only_change': 'isolated r5 output prefix', 'physics_unchanged': True})
        case['prompt'] = case['prompt'].replace('candidate-' + old, 'candidate-' + new)
    cases.sort(key=lambda case: (case['case_id'] in {'mpinat-benchpep', 'mpinat-benchpep-h'},
                                sizes[case['case_id']], case['case_id']))
    manifest.update(candidate_image=R5_IMAGE, candidate_source=R5_SOURCE, cases=cases,
                    prepared_only=True, new_gpu_submissions_authorized=False,
                    excluded_native_complete=sorted(COMPLETED | {'mpinat-benchmem'}),
                    ordering='Small input archives first; PEP/PEP-h last. Size is a scheduling proxy, not measured runtime.')
    args.output.mkdir(parents=True)
    save_new(args.output / 'remaining-mpinat-r5-manifest.json', manifest)
    receipt = {'schema': 'fs2.agent-input-continuation/v1', 'candidate_image': R5_IMAGE,
               'candidate_source': R5_SOURCE, 'cases': [case['case_id'] for case in cases],
               'files': files, 'parameters': parameters, 'completed_mem_verification': str(args.verification),
               'new_gpu_submissions': 0, 'previous_artifacts_modified': False}
    save_new(args.output / 'staging.json', receipt)
    return receipt


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('previous', 'manifest', 'verification', 'workspace', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    result = prepare(parser.parse_args())
    print(json.dumps({'cases': result['cases'], 'copied_public_files': len(result['files']),
                      'new_gpu_submissions': 0}))


if __name__ == '__main__':
    main()
