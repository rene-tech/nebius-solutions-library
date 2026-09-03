#!/usr/bin/env bash
# Offline checks for the Complexa artifact ingestion.
#
# The promotion and reclaim suites need the reviewed localization core. Once it
# is committed in this tree they run against it and cannot be skipped or
# shadowed by an override. Until then, FS2_LOCALIZATION_ADAPTERS may point at
# the directory holding its localization.py and primitives.py.
#
# This script reports which source was used and refuses to exit green on a
# skipped promotion suite when the core is present, so "OK" never hides the one
# thing these checks exist to exercise.
set -euo pipefail

cd "$(dirname "$0")"

python3 -m py_compile fetch_artifacts.py promote_generations.py reclaim_staging.py \
    render_ingestion_jobs.py probes/complexa_loader_probe.py probes/generation_visibility_probe.py

python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'tests')
import fetch_artifacts
from test_ingestion import localization_source, adapter_digests

document = fetch_artifacts.load_contract(pathlib.Path('ingestion-contract.json'))
print('contract valid:', len(document['artifacts']), 'artifacts,',
      sum(a['tree']['total_bytes'] for a in document['artifacts']), 'bytes')

adapters, origin = localization_source()
print(f'localization core: {origin}' + (f' at {adapters}' if adapters else ''))
if adapters is not None:
    for name, digest in sorted(adapter_digests(adapters).items()):
        print(f'  {name}: {digest}')
if origin == 'absent':
    print('  WARNING: promotion and reclaim suites will skip; they are not exercised by this run.')
PY

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v 2>&1 | tee /tmp/fs2-complexa-checks.$$
status=${PIPESTATUS[0]}

python3 - "/tmp/fs2-complexa-checks.$$" <<'PY'
import pathlib, re, sys
report = pathlib.Path(sys.argv[1]).read_text()
pathlib.Path(sys.argv[1]).unlink(missing_ok=True)
sys.path.insert(0, 'tests')
from test_ingestion import localization_source

skipped = re.findall(r'^(\S+) \(([^)]+)\) \.\.\. skipped', report, re.M)
if localization_source()[1] != 'absent':
    offenders = [name for name, case in skipped
                 if 'PromotionTests' in case or 'ReclaimTests' in case]
    if offenders:
        print('FAIL: the localization core is available but these skipped: '
              + ', '.join(offenders))
        raise SystemExit(1)
print(f'skipped: {len(skipped)}')
PY

exit "$status"
