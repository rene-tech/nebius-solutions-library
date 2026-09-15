#!/usr/bin/env python3
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess

KUBE = '/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig'
ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('pod')
args = parser.parse_args()
out = ROOT / 'raw' / args.pod
out.mkdir(parents=True, exist_ok=True)
for label, command in [('pod.json', ['get', 'pod', args.pod, '-o', 'json']), ('events.json', ['get', 'events', '--field-selector', 'involvedObject.name=' + args.pod, '-o', 'json']), ('server.log', ['logs', args.pod]), ('gpu.txt', ['exec', args.pod, '--', 'nvidia-smi'])]:
    p = subprocess.run(['kubectl', '--kubeconfig', KUBE, '-n', 'fs2-bioir-openfold', *command], text=True, capture_output=True)
    (out / label).write_text(p.stdout + p.stderr)
python_command = ['python', '-m', 'pip', 'freeze']
if args.pod.startswith(('openfold3-baseline', 'openfold3-upstream-resident')):
    python_command = ['/bin/bash', '-ec', 'source /opt/fs2/activate.sh; python -m pip freeze']
p = subprocess.run(['kubectl', '--kubeconfig', KUBE, '-n', 'fs2-bioir-openfold', 'exec', args.pod, '--', *python_command], text=True, capture_output=True)
(out / 'pip-freeze.txt').write_text(p.stdout + p.stderr)
pod_data = json.loads((out / 'pod.json').read_text())
for container in pod_data['spec'].get('initContainers', []):
    p = subprocess.run(['kubectl', '--kubeconfig', KUBE, '-n', 'fs2-bioir-openfold', 'logs', args.pod, '-c', container['name']], text=True, capture_output=True)
    (out / ('init-' + container['name'] + '.log')).write_text(p.stdout + p.stderr)
(out / 'collected.json').write_text(json.dumps({'utc': dt.datetime.now(dt.timezone.utc).isoformat()}) + '\n')
