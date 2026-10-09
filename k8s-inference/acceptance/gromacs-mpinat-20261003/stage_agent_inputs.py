"""Stage only public, hash-bound GPU test inputs; never admit or execute work.

Keep private customer fixtures in their existing read-only mounts. The staging
receipt distinguishes retained original inputs from new run identities. Template
files use read-only mode; this is not an immutable mount or a GPU qualification.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil


def load(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o444)


def copy_checked(source, destination, expected_hash, expected_size):
    if (not source.is_file() or source.is_symlink()
            or source.stat().st_size != expected_size or sha(source) != expected_hash):
        raise ValueError('Original public fixture differs from its recorded identity.')
    if destination.exists():
        raise FileExistsError('Staging preserves existing files; choose a fresh target.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Open exclusively so a concurrent appearance cannot be silently replaced.
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    if sha(destination) != expected_hash:
        raise ValueError('Staged fixture readback does not match its source.')
    destination.chmod(0o444)
    return {'source': str(source), 'target': str(destination), 'sha256': expected_hash,
            'size_bytes': expected_size, 'file_mode': '0444'}


def rebase_remaining(remaining, reconciliation, image, source):
    if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', image) or not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('Use immutable candidate image/source identities.')
    complete = {row['case_id'] for row in reconciliation['coverage']
                if row['native_benchmark_complete']}
    expected = {row['case_id'] for row in reconciliation['coverage']
                if row['scientific_case'] and not row['native_benchmark_complete']}
    ids = [row['case_id'] for row in remaining['cases']]
    if len(ids) != len(set(ids)) or set(ids) != expected or set(ids) & complete:
        raise ValueError('Remaining cases must exclude all already native-complete work.')
    value = copy.deepcopy(remaining)
    old = remaining['candidate_image'].split(':')[-1][:8]
    new = image.split(':')[-1][:8]
    for row in value['cases']:
        if not re.fullmatch(r'mpinat-[a-z0-9-]+', row['case_id']):
            raise ValueError('Unexpected public benchmark case identifier.')
        row['prompt'] = row['prompt'].replace('candidate-' + old, 'candidate-' + new)
    value.update(candidate_image=image, candidate_source=source,
                 prepared_only=True, new_gpu_submissions_authorized=False,
                 excluded_native_complete=sorted(complete))
    return value, old, new


def stage(args):
    workspace, output = args.workspace.resolve(), args.output.resolve()
    targets = [workspace / 'inputs/mpinat', workspace / 'examples/v3', output]
    if any(path.exists() for path in targets):
        raise FileExistsError('Public fixture targets and output receipt must be fresh.')
    remaining, old, new = rebase_remaining(load(args.remaining), load(args.reconciliation),
                                          args.image, args.source)
    suite = {row['id']: row for row in load(args.fixtures / 'suite.json')['cases']}
    manifest = load(args.starter_pack / 'manifest.json')
    if (manifest['schema'] != 'fs2-serve.nebius.ai/customer-starter-pack/v1'
            or manifest['version'] != 'v3' or manifest['release_status'] != 'qualified'):
        raise ValueError('Expected the retained qualified v3 seed, not a generated replacement.')
    files, parameters = [], []
    for case in remaining['cases']:
        identifier = case['case_id'].removeprefix('mpinat-')
        entry = suite[identifier]
        base = workspace / 'inputs/mpinat' / identifier
        files.append(copy_checked(args.fixtures / identifier / 'input.tar.gz', base / 'input.tar.gz',
                                  entry['bundle_sha256'], entry['bundle_bytes']))
        parameter_source = args.parameters / identifier / 'parameters-candidate.json'
        original = load(parameter_source)
        revised = {**original, 'output_prefix': original['output_prefix'].replace(
            'candidate-' + old, 'candidate-' + new)}
        target = base / 'parameters-candidate.json'
        save_new(target, revised)
        parameters.append({'case_id': case['case_id'], 'source': str(parameter_source),
                           'source_sha256': sha(parameter_source), 'target': str(target),
                           'sha256': sha(target), 'only_change': 'isolated r4 output prefix',
                           'max_output_bytes': revised['max_output_bytes']})
        # Cheap source metadata, useful without extracting large public TPRs.
        provenance = args.fixtures / identifier / 'provenance.json'
        files.append(copy_checked(provenance, base / 'provenance.json', sha(provenance), provenance.stat().st_size))
    for row in manifest['objects']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Seed path escapes the recorded public pack.')
        files.append(copy_checked(args.starter_pack / relative, workspace / 'examples/v3' / relative,
                                  row['sha256'], row['size_bytes']))
    original_manifest = args.starter_pack / 'manifest.json'
    files.append(copy_checked(original_manifest, workspace / 'examples/v3/manifest.json',
                              sha(original_manifest), original_manifest.stat().st_size))
    output.mkdir(parents=True)
    save_new(output / 'remaining-mpinat-r4-manifest.json', remaining)
    hosted = load(args.hosted)
    if len(hosted['cases']) != 1 or hosted['cases'][0]['case_id'] != 'assumed-hosted-gromacs':
        raise ValueError('Only the unchanged assumed hosted GROMACS case is staged.')
    hosted.update(candidate_image=args.image, candidate_source=args.source, prepared_only=True,
                  new_gpu_submissions_authorized=False)
    save_new(output / 'hosted-alanine-r4-manifest.json', hosted)
    receipt = {'schema': 'fs2.agent-input-staging/v1', 'prepared_only': True,
               'candidate_image': args.image, 'candidate_source': args.source,
               'workspace': str(workspace), 'mpinat_cases': len(remaining['cases']),
               'excluded_native_complete': remaining['excluded_native_complete'],
               'hosted_cases': ['assumed-hosted-gromacs'], 'parameters': parameters,
               'files': files, 'new_gpu_submissions': 0,
               'limitations': ['Local public fixtures only; no private data or credentials copied.',
                              '0444 file mode is not an immutable container mount.',
                              'Coordinator must assign GPU admission; staging is not execution.']}
    save_new(output / 'staging.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace', 'output', 'remaining', 'reconciliation', 'fixtures', 'parameters',
                 'starter-pack', 'hosted'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--source', required=True)
    result = stage(parser.parse_args())
    print(json.dumps({'mpinat_cases': result['mpinat_cases'], 'hosted_cases': result['hosted_cases'],
                      'excluded_native_complete': result['excluded_native_complete'],
                      'files': len(result['files']), 'new_gpu_submissions': 0}))


if __name__ == '__main__':
    main()
