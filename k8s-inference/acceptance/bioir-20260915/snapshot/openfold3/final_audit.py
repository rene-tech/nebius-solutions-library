#!/usr/bin/env python3
"""Read-only post-cleanup allocation, process and GPU-memory receipt."""
import csv
import json
import time
from control import NODE, k, save, preflight

preflight('fs2-bioir-of3-final-memory-audit')
pods = json.loads(k('get', 'pods', '-A', '-o', 'json'))['items']
detector = next(p['metadata']['name'] for p in pods if p['spec'].get('nodeName') == NODE and p['metadata']['name'].startswith('nebius-node-problem-detector-gpu-'))
fields = ['name', 'uuid', 'driver_version', 'memory.used', 'utilization.gpu']
raw = k('-n', 'kube-system', 'exec', detector, '--', '/usr/bin/nvidia-smi', '--query-gpu=' + ','.join(fields), '--format=csv,noheader,nounits')
rows = [dict(zip(fields, (v.strip() for v in row))) for row in csv.reader(raw.splitlines())]
save('lifecycle/final-gpu-memory.json', {'unix': time.time(), 'node': NODE, 'gpus': rows, 'read_only': True})
assert len(rows) == 1 and all(float(row['memory.used']) == 0 and float(row['utilization.gpu']) == 0 for row in rows), 'GPU not fully idle; preserve receipt and escalate without reset'
print(json.dumps({'node': NODE, 'gpus': rows, 'gpu_fully_idle': True}))
