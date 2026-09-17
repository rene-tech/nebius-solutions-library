#!/usr/bin/env python3
"""Delete only a named lane-owned pod after evidence collection, retaining time."""
import argparse
import datetime as dt
import json
import subprocess
from prepare_baseline import KUBECONFIG, NAMESPACE, ROOT, NODES

parser = argparse.ArgumentParser()
parser.add_argument('pod')
args = parser.parse_args()
cmd = ['kubectl', '--kubeconfig', KUBECONFIG, '-n', NAMESPACE]
pod = json.loads(subprocess.check_output(cmd + ['get', 'pod', args.pod, '-o', 'json']))
assert pod['metadata']['labels']['evaluation'] == 'fs2-bioir-20260915'
assert pod['metadata']['labels']['lane'] == 'openfold'
assert pod['spec'].get('nodeName') in NODES.values()
assert (ROOT / 'raw' / args.pod / 'pod.json').exists(), 'Collect evidence before deleting'
receipt = {'pod': args.pod, 'uid': pod['metadata']['uid'], 'requested_at': dt.datetime.now(dt.timezone.utc).isoformat()}
subprocess.run(cmd + ['delete', 'pod', args.pod, '--wait=true'], check=True)
receipt['deletion_observed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
(ROOT / 'raw' / args.pod / 'deletion.json').write_text(json.dumps(receipt, indent=2) + '\n')
