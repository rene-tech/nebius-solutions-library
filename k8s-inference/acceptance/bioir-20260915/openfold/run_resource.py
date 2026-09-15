#!/usr/bin/env python3
"""Wait for task pod readiness, execute full HTTP matrix and retain evidence."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from prepare_baseline import ROOT, KUBECONFIG, NAMESPACE

parser = argparse.ArgumentParser()
parser.add_argument('pod')
parser.add_argument('--variant', required=True)
args = parser.parse_args()
model = args.pod.split('-')[0]
cmd = ['kubectl', '--kubeconfig', KUBECONFIG, '-n', NAMESPACE]
def run(*parts):
    return subprocess.run(cmd + list(parts), check=True)
deadline = time.monotonic() + 900
while time.monotonic() < deadline:
    pod = json.loads(subprocess.check_output(cmd + ['get', 'pod', args.pod, '-o', 'json']))
    assert pod['metadata']['labels']['lane'] == 'openfold'
    if any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod['status'].get('conditions', [])):
        break
    if pod['status']['phase'] in ('Failed', 'Succeeded'):
        raise RuntimeError('Pod terminated before readiness')
    time.sleep(5)
else:
    raise TimeoutError('Pod not Ready after 900 seconds')
for name in ('benchmark_http.py', 'probe_contract.py'):
    run('cp', str(ROOT / name), args.pod + ':/tmp/' + name)
run('cp', str(ROOT / 'fixtures/cases.json'), args.pod + ':/tmp/cases.json')
python = ['/bin/bash', '-ec', 'source /opt/fs2/activate.sh; exec python "$@"', '--'] if args.pod.startswith(('openfold3-baseline', 'openfold3-upstream-resident')) else ['python']
run('exec', args.pod, '--', *python, '/tmp/benchmark_http.py', '--model', model, '--variant', args.variant, '--cases', '/tmp/cases.json', '--output', '/tmp/bench-results', '--stop-after-failure')
run('exec', args.pod, '--', *python, '/tmp/probe_contract.py', '--model', model, '--cases', '/tmp/cases.json', '--output', '/tmp/bench-results/feature-probes.json')
run('cp', args.pod + ':/tmp/bench-results', str(ROOT / 'raw' / args.pod))
subprocess.run(['python3', str(ROOT / 'collect_resource.py'), args.pod], check=True)
