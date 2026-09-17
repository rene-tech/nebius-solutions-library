#!/usr/bin/env python3
"""Real Preview2 requests retaining the native preprocessing, seed and scoring."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
from benchmark_http import payload, validate

p = argparse.ArgumentParser()
p.add_argument('--cases', required=True)
p.add_argument('--out', required=True)
p.add_argument('--samples', type=int, default=1)
a = p.parse_args()
assert a.samples == 1
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
fixtures = {c['id']: c for c in json.loads(Path('/snapshot-app/cases.json').read_text())}
for index, name in enumerate(a.cases.split(',')):
    case = fixtures[name]
    request = payload('openfold3', case, 'snapshot-' + name + '-' + str(index))
    row = {'fixture': name, 'request': request, 'started_unix': time.time()}
    start = time.perf_counter()
    try:
        req = urllib.request.Request('http://127.0.0.1:8000/biology/openfold/openfold3/predict', data=json.dumps(request).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=1200) as response:
            result = json.load(response)
        row['http_seconds'] = time.perf_counter() - start
        (out / f'{index}-{name}.response.json').write_text(json.dumps(result))
        row.update(valid=True, quality=validate('openfold3', case, result))
        with urllib.request.urlopen('http://127.0.0.1:8000/v1/runtime') as response:
            health = json.load(response)
        (out / f'{index}-{name}.runtime.json').write_text(json.dumps(health, indent=2))
    except Exception as exc:
        row.update(valid=False, error_type=type(exc).__name__, error=str(exc))
    row['validated_seconds'] = time.perf_counter() - start
    row['completed_unix'] = time.time()
    with (out / 'attempts.jsonl').open('a') as stream:
        stream.write(json.dumps(row) + '\n')
    print(json.dumps(row), flush=True)
