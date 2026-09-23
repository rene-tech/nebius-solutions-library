"""Retain GROMACS Runs/Usage/Logs through the existing operator API, read-only."""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--from-at', default='2026-09-23T07:45:00Z')
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    secret = json.loads(subprocess.check_output(['kubectl', '--kubeconfig', args.kubeconfig,
        '--context', args.context, '-n', 'fs2-system', 'get', 'secret', 'fs2-serve-admin', '-o', 'json']))
    token = base64.b64decode(secret['data']['token']).decode().strip()
    origin = 'https://89.169.99.188'
    with httpx.Client(base_url=origin, timeout=90, trust_env=False, headers={'origin': origin}) as client:
        response = client.post('/admin/api/v1/session', headers={'authorization': 'Bearer ' + token})
        response.raise_for_status()
        window = {'from': args.from_at, 'to': datetime.now(timezone.utc).isoformat()}
        response = client.get('/admin/api/v1/apps', params=window)
        response.raise_for_status()
        apps = [item for item in response.json()['data']['items'] if item['model_id'] == 'gromacs']
        if len(apps) != 1:
            raise RuntimeError(f'Expected one GROMACS App, found {len(apps)}')
        app = apps[0]
        for path in ('runs', 'usage', 'logs', 'settings'):
            response = client.get(f'/admin/api/v1/apps/{app["id"]}/{path}',
                params={**window, **({'limit': 500} if path == 'logs' else {})})
            response.raise_for_status()
            value = response.json()
            target = args.output / (path + '.json')
            with target.open('x') as handle:
                target.chmod(0o600)
                json.dump(value, handle, indent=2)
                handle.write('\n')
            print(json.dumps({'path': path, 'state': value.get('data', {}).get('state'),
                'items': len(value.get('data', {}).get('items', []))}))
        client.delete('/admin/api/v1/session')


if __name__ == '__main__':
    main()
