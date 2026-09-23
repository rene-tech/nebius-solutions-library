"""Run the unmodified customer scientific-batch client with a private key file."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True)
    parser.add_argument('--client', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--idempotency-key', required=True)
    parser.add_argument('--wait-seconds', default='900')
    args = parser.parse_args()
    value = json.loads(args.key_file.read_text())
    env = {**os.environ, 'SCIENTIFIC_MODELS_MCP_URL': 'https://89.169.99.188/mcp',
           'SCIENTIFIC_MODELS_API_KEY': value['secret']}
    command = [args.python, str(args.client), '--model', 'gromacs', '--tool', 'submit_gromacs_workflow',
               '--operation', 'run-workflow', '--source', str(args.fixture / 'input.tar.gz'),
               '--parameters', str(args.fixture / 'request.json'), '--entry-name', 'gromacs-inputs',
               '--semantic-type', 'gromacs-input-bundle/v1', '--media-type', 'application/x-tar',
               '--compression', 'gzip', '--output', str(args.output), '--idempotency-key', args.idempotency_key,
               '--display-name', 'GROMACS ' + args.fixture.name, '--wait-seconds', args.wait_seconds,
               '--poll-seconds', '5']
    raise SystemExit(subprocess.run(command, env=env).returncode)


if __name__ == '__main__':
    main()
