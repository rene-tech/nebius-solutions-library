"""Generate customer-path capacity/recovery/I/O fixtures from a qualified TPR.

Repeated jobs intentionally use identical physics and state for operational
comparison. They are NOT independent scientific replicas or a converged study.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tpr', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=1, choices=range(1, 11))
    parser.add_argument('--steps', type=int, default=200000)
    parser.add_argument('--segment-minutes', type=float, default=0.5)
    parser.add_argument('--large-trace', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    with tarfile.open(args.output / 'input.tar.gz', 'w:gz') as archive:
        archive.add(args.tpr, arcname='source.tpr')
    steps = [
        {'id': 'set-length', 'command': 'convert-tpr',
         'args': ['-s', 'source.tpr', '-o', 'md.tpr', '-nsteps', str(args.steps)]},
        {'id': 'production', 'command': 'mdrun', 'args': ['-s', 'md.tpr', '-deffnm', 'md']},
        {'id': 'join-trajectory', 'command': 'trjcat',
         'args': ['-f', {'files': 'md.part*.xtc'}, '-o', 'md.xtc']},
        {'id': 'check', 'command': 'check', 'args': ['-f', 'md.xtc']},
    ]
    if args.large_trace:
        steps += [
            {'id': 'export-trr', 'command': 'trjconv',
             'args': ['-f', 'md.xtc', '-s', 'md.tpr', '-o', 'md.trr'], 'stdin': 'System\n',
             'expected_outputs': ['md.trr']},
            {'id': 'check-trr', 'command': 'check', 'args': ['-f', 'md.trr']},
        ]
    request = {'schema': 'fs2-serve.nebius.ai/gromacs-workflow-request/v1',
               'jobs': [{'id': f'throughput-{index + 1:02}', 'steps': steps} for index in range(args.jobs)],
               'segment_minutes': args.segment_minutes, 'checkpoint_minutes': 0.1,
               'max_wall_seconds': 3600}
    (args.output / 'request.json').write_text(json.dumps(request, indent=2) + '\n')
    with args.tpr.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    (args.output / 'fixture.json').write_text(json.dumps({
        'source_tpr_sha256': digest, 'steps': args.steps, 'jobs': args.jobs,
        'purpose': 'Operational repeatability/queue/recovery/transport; not independent sampling.',
        'trr_note': 'Optional TRR is a format-converted XTC for transport testing, not recovered precision.',
    }, indent=2) + '\n')
    print(str(args.output))


if __name__ == '__main__':
    main()
