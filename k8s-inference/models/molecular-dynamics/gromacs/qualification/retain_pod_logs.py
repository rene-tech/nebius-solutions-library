"""Read Loki evidence for an exact qualification Pod after normal Job cleanup."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'fs2-workflow-[a-z0-9-]+', args.pod):
        raise ValueError('Use the exact task-owned scientific workflow Pod.')
    query = '{k8s_namespace_name="fs2-models",k8s_pod_name="' + args.pod + '"}'
    script = """import json,urllib.parse,urllib.request
params={'query':%r,'since':'4h','limit':'2000','direction':'forward'}
url='http://fs2-loki.fs2-observability.svc:3100/loki/api/v1/query_range?'+urllib.parse.urlencode(params)
with urllib.request.urlopen(url,timeout=90) as response: value=json.load(response)
rows=[{'at':t,'container':s['stream'].get('k8s_container_name'),'message':m}
      for s in value['data']['result'] for t,m in s['values']]
print(json.dumps(sorted(rows,key=lambda x:x['at'])))
""" % query
    raw = subprocess.check_output(['kubectl', '--kubeconfig', args.kubeconfig, '--context', args.context,
        '-n', 'fs2-system', 'exec', 'deploy/fs2-serve-control-plane', '-c', 'control-plane',
        '--', 'python', '-c', script])
    rows = json.loads(raw)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        json.dump(rows, output, indent=2)
    print(json.dumps({'pod': args.pod, 'retained_lines': len(rows)}))


if __name__ == '__main__':
    main()
