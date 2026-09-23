"""Prepare the public NVIDIA-referenced STMV performance fixture, unchanged science.

Source: https://doi.org/10.5281/zenodo.3893789 (Pall et al., 2020).
Only run length changes for a bounded performance probe. Identical-state repeats
are not independent scientific samples. Data stays in private test storage.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=20000)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 3 or not 1000 <= args.steps <= 100000:
        raise ValueError('This qualification fixture is deliberately bounded.')
    with args.archive.open('rb') as source:
        assert hashlib.file_digest(source, 'md5').hexdigest() == '3b142fe31972077474967636ab4c69e0'
    args.output.mkdir(mode=0o700, exist_ok=False)
    data = args.output / 'data'
    data.mkdir()
    with tarfile.open(args.archive, 'r:gz') as archive:
        members = [member for member in archive.getmembers() if member.name.endswith('/stmv/topol.tpr')]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 256 * 1024**2:
            raise ValueError('Expected one bounded regular STMV input; inspect the pinned archive.')
        with archive.extractfile(members[0]) as source, (data / 'source.tpr').open('xb') as target:
            shutil.copyfileobj(source, target, 1024**2)
    with tarfile.open(args.output / 'input.tar.gz', 'w:gz') as archive:
        archive.add(data / 'source.tpr', arcname='source.tpr')
    steps = [{'id': 'set-length', 'command': 'convert-tpr',
              'args': ['-s', 'source.tpr', '-o', 'md.tpr', '-nsteps', str(args.steps)]}]
    steps.extend({'id': f'benchmark-{index + 1}', 'command': 'mdrun',
                  'args': ['-s', 'md.tpr', '-deffnm', f'md-{index + 1}']} for index in range(args.repetitions))
    request = {'schema': 'fs2-serve.nebius.ai/gromacs-workflow-request/v1',
               'jobs': [{'id': 'stmv', 'steps': steps}], 'max_wall_seconds': 3600,
               'segment_minutes': 5, 'checkpoint_minutes': 5}
    (args.output / 'request.json').write_text(json.dumps(request, indent=2) + '\n')
    with (data / 'source.tpr').open('rb') as source:
        sha = hashlib.file_digest(source, 'sha256').hexdigest()
    (args.output / 'fixture.json').write_text(json.dumps({'source': 'https://doi.org/10.5281/zenodo.3893789',
        'source_tpr_sha256': sha, 'steps': args.steps, 'repetitions': args.repetitions,
        'scientific_protocol_changed': False, 'run_length_changed_for_benchmark': True}, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'source_tpr_sha256': sha}))


if __name__ == '__main__':
    main()
